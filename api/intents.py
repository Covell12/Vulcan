"""POST /intents, POST /intents/{id}/answers: photos + annotation + text ->
IntentSpec, then user answers to fill in the critical dimensions.

Persistence is deliberately boring, per CLAUDE.md: one JSON file per intent
under data/intents/, no database yet. Uploaded photo bytes are NOT persisted
— they're only needed transiently to call the vision provider; the browser
already holds them for redisplay during the same session.

This module owns the one rule that's non-negotiable per CLAUDE.md: a
fit-critical dimension may only ever commit from source="user_measured", and
`status` can only become "ready_for_design" once every critical dimension for
the chosen template has been. That gate is computed here, in code, every
time the IntentSpec changes — never trusted from the vision provider's own
opinion of it (see `_apply_critical_dim_gate`).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import jsonschema
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

import templates_lib  # noqa: F401  (import side effect: populates the registry)
from api.depth_provider import DepthProviderError, ScaleRegion, estimate_scale
from api.param_schema import form_fields_for
from api.photo import PhotoInput
from api.vision_provider import VisionProviderError, parse_intent
from templates_lib.registry import TemplateSpec, all_templates, get_template

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
INTENTS_DIR = DATA_DIR / "intents"
SCHEMA_PATH = BASE_DIR / "schemas" / "intent_spec.schema.json"

MAX_PHOTOS = 3
MATERIAL_CHOICES = ("PLA", "PETG", "TPU", "CF-PETG")

# CLAUDE.md rule 3: a user-typed value that differs from the depth prior by more
# than this fraction is treated as a likely unit mistake and re-asked, not
# silently committed.
MISMATCH_THRESHOLD = 0.20


def _load_schema() -> dict[str, Any]:
    with open(SCHEMA_PATH) as f:
        return json.load(f)


_SCHEMA = _load_schema()


class AnswerInput(BaseModel):
    question_id: str
    measure_mm: float | None = None
    choice: str | None = None
    confirm: bool | None = None


class AnswersRequest(BaseModel):
    answers: list[AnswerInput]


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _validation_errors(intent: dict[str, Any]) -> list[str]:
    validator = jsonschema.Draft202012Validator(_SCHEMA)
    return [
        f"{'.'.join(str(p) for p in e.absolute_path)}: {e.message}"
        for e in validator.iter_errors(intent)
    ]


# ---------------------------------------------------------------------------
# Persistence (JSON files under data/intents/ — no database yet)
# ---------------------------------------------------------------------------


def _intent_path(intent_id: str) -> Path:
    return INTENTS_DIR / f"{intent_id}.json"


def _save_intent(intent: dict[str, Any]) -> None:
    INTENTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(_intent_path(intent["intent_id"]), "w") as f:
        json.dump(intent, f, indent=2)


def _load_intent(intent_id: str) -> dict[str, Any]:
    path = _intent_path(intent_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No intent '{intent_id}'.")
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Template catalog (what the vision provider is told is buildable)
# ---------------------------------------------------------------------------


def _template_catalog() -> list[dict[str, Any]]:
    return [
        {
            "template_id": spec.template_id,
            "label": spec.label,
            "category": spec.category,
            "critical_dims": list(spec.critical_dims),
            "params": form_fields_for(spec.params_model),
        }
        for spec in sorted(all_templates().values(), key=lambda s: s.template_id)
    ]


# ---------------------------------------------------------------------------
# Critical-dim gate (the non-negotiable rule)
# ---------------------------------------------------------------------------


def _apply_critical_dim_gate(intent: dict[str, Any]) -> dict[str, Any]:
    template_id = intent.get("template_id")
    spec = get_template(template_id) if template_id else None

    if spec is not None:
        _ensure_critical_dimensions(intent, spec)
        _ensure_critical_questions(intent, spec)

    if intent.get("out_of_scope_reason"):
        intent["status"] = "out_of_scope"
    elif spec is not None:
        dims_by_name = {d["name"]: d for d in intent["dimensions"]}
        all_confirmed = all(
            dims_by_name.get(name, {}).get("source") == "user_measured"
            for name in spec.critical_dims
        )
        intent["status"] = "ready_for_design" if all_confirmed else "needs_answers"
    else:
        intent["status"] = "needs_answers"

    return intent


def _ensure_critical_dimensions(intent: dict[str, Any], spec: TemplateSpec) -> None:
    dims_by_name = {d["name"]: d for d in intent.setdefault("dimensions", [])}
    for name in spec.critical_dims:
        dim = dims_by_name.get(name)
        if dim is None:
            dim = {
                "name": name,
                "value_mm": None,
                "source": "assumed",
                "confidence": 0.0,
                "critical": True,
                "cross_check": None,
            }
            intent["dimensions"].append(dim)
            dims_by_name[name] = dim

    # Never trust the provider's own opinion of `critical` — only
    # templates_lib.registry's critical_dims decides, in both directions:
    # force it True for every actual critical dim (including ones just
    # synthesized above) and False for everything else, even if the
    # provider marked some other dimension critical on its own.
    for dim in intent["dimensions"]:
        dim["critical"] = dim["name"] in spec.critical_dims


def _ensure_critical_questions(intent: dict[str, Any], spec: TemplateSpec) -> None:
    questions = intent.setdefault("questions", [])
    asked_dims = {q.get("dim_name") for q in questions}
    for name in spec.critical_dims:
        if name in asked_dims:
            continue
        pretty = name.removesuffix("_mm").replace("_", " ")
        questions.append(
            {
                "question_id": f"auto-{name}",
                "dim_name": name,
                "prompt": f"What is the {pretty}, in mm?",
                "kind": "measure_mm",
                "choices": None,
                "overlay": None,
            }
        )


# ---------------------------------------------------------------------------
# Depth prior (M4): fill assumed dims with metric-depth proposals
# ---------------------------------------------------------------------------


def _build_scale_regions(intent: dict[str, Any]) -> list[ScaleRegion]:
    """One region per question that has a dim_name and an overlay with points —
    the overlay is where on the photo that dimension should be measured, so it
    doubles as where to sample the depth prior."""
    regions: list[ScaleRegion] = []
    for q in intent.get("questions", []):
        dim_name = q.get("dim_name")
        overlay = q.get("overlay")
        if not dim_name or not overlay:
            continue
        points = overlay.get("points")
        if not points:
            continue
        regions.append(
            ScaleRegion(
                dim_name=dim_name,
                shape=overlay.get("shape") or "line",
                points=points,
                photo_index=overlay.get("photo_index", 0) or 0,
            )
        )
    return regions


def _apply_depth_prior(intent: dict[str, Any], photos: list[PhotoInput]) -> None:
    """After the vision pass, propose metric values for dims still on source
    "assumed", turning them into "depth_inferred" suggestions. Critical dims
    stay un-committed (only "user_measured" satisfies the gate) — this only
    prefills the number the UI shows as "looks like ~X — measure to confirm".

    Depth is an OPTIONAL prior: any provider failure degrades to no proposals
    rather than failing intent creation (CLAUDE.md: the system must work fully
    without depth)."""
    regions = _build_scale_regions(intent)
    if not regions:
        return

    by_photo: dict[int, list[ScaleRegion]] = {}
    for region in regions:
        by_photo.setdefault(region.photo_index, []).append(region)

    estimates: dict[str, Any] = {}
    try:
        for photo_index, photo_regions in by_photo.items():
            if not (0 <= photo_index < len(photos)):
                continue
            for est in estimate_scale(photos[photo_index], photo_regions):
                estimates.setdefault(est.dim_name, est)  # first estimate wins
    except DepthProviderError:
        return  # degrade gracefully — no depth proposals this time

    for dim in intent.get("dimensions", []):
        if dim.get("source") != "assumed":
            continue
        est = estimates.get(dim["name"])
        if est is None:
            continue
        dim["value_mm"] = est.value_mm
        dim["source"] = "depth_inferred"
        dim["confidence"] = est.confidence


# ---------------------------------------------------------------------------
# Cross-check (M4): a user measurement that disagrees with the depth prior by
# >20% is re-asked, never silently committed or overridden (CLAUDE.md rule 3).
# ---------------------------------------------------------------------------


def _depth_prior_for(dim: dict[str, Any]) -> float | None:
    """The depth prior for a dim, stable across the answer lifecycle: once a
    mismatch is flagged it lives in cross_check.depth_value_mm; before any
    answer it's the depth_inferred value_mm."""
    cc = dim.get("cross_check")
    if cc and cc.get("depth_value_mm") is not None:
        return cc["depth_value_mm"]
    if dim.get("source") == "depth_inferred" and dim.get("value_mm") is not None:
        return dim["value_mm"]
    return None


def _is_flagged(dim: dict[str, Any]) -> bool:
    cc = dim.get("cross_check")
    return bool(cc and cc.get("status") == "mismatch_reask")


def _approx_equal(a: float | None, b: float | None, tol: float = 1e-6) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol * max(1.0, abs(b))


def _commit_measurement(
    dim: dict[str, Any], value: float, depth: float | None, status: str
) -> None:
    dim["value_mm"] = value
    dim["source"] = "user_measured"
    dim["confidence"] = 1.0
    dim["cross_check"] = {
        "depth_value_mm": depth,
        "ratio": round(value / depth, 4) if depth else None,
        "status": status,
    }


def _num(x: float) -> str:
    return f"{x:g}"


def _unit_mistake_hint(measured: float, depth: float) -> str:
    """Name the most likely unit slip behind a big measured-vs-depth gap."""
    if measured <= 0 or depth <= 0:
        return "Please double-check the value."
    bigger = depth / measured  # photo thinks it's this many times bigger
    smaller = measured / depth  # ...or this many times smaller
    if 8 <= bigger <= 12:
        return f"Did you measure in centimeters? {_num(measured)}cm is {_num(measured * 10)}mm."
    if 8 <= smaller <= 12:
        return f"Did you enter millimeters but mean centimeters? {_num(measured)}mm is {_num(measured / 10)}cm."
    if 22 <= bigger <= 28:
        return f"Did you measure in inches? {_num(measured)}in is {_num(round(measured * 25.4, 1))}mm."
    if 22 <= smaller <= 28:
        return "Did you mix up inches and millimeters?"
    return (
        "That's a big difference — please double-check your measurement and its units."
    )


def _reask_question_id(dim_name: str) -> str:
    return f"reask-{dim_name}"


def _set_reask_question(
    intent: dict[str, Any], dim_name: str, measured: float, depth: float
) -> None:
    """Add (or refresh) a re-ask question naming both values + the likely unit
    mistake. Idempotent per dim so re-flagging doesn't pile up questions."""
    prompt = (
        f"You entered {_num(measured)}, but the photo suggests about {_num(depth)}mm. "
        f"{_unit_mistake_hint(measured, depth)} "
        f"Enter a corrected value, or re-submit {_num(measured)} to confirm your "
        "measurement is right."
    )
    qid = _reask_question_id(dim_name)
    questions = intent.setdefault("questions", [])
    for q in questions:
        if q.get("question_id") == qid:
            q["prompt"] = prompt
            return
    questions.append(
        {
            "question_id": qid,
            "dim_name": dim_name,
            "prompt": prompt,
            "kind": "measure_mm",
            "choices": None,
            "overlay": None,
        }
    )


def _prune_reask_question(intent: dict[str, Any], dim_name: str) -> None:
    qid = _reask_question_id(dim_name)
    questions = intent.get("questions")
    if not questions:
        return
    intent["questions"] = [q for q in questions if q.get("question_id") != qid]


def _cross_check_measurement(
    intent: dict[str, Any], dim: dict[str, Any], measured: float
) -> None:
    """Apply a measure_mm answer through the cross-check. Commits, flags for
    re-ask, or accepts an override — never silently overriding the user."""
    depth = _depth_prior_for(dim)

    # No depth prior available -> nothing to cross-check against; commit.
    if depth is None or depth <= 0:
        _commit_measurement(dim, measured, None, status="unavailable")
        _prune_reask_question(intent, dim["name"])
        return

    # Re-submitting the exact value that was just flagged = an explicit override.
    if _is_flagged(dim) and _approx_equal(measured, dim.get("value_mm")):
        _commit_measurement(dim, measured, depth, status="ok")
        _prune_reask_question(intent, dim["name"])
        return

    relative_diff = abs(measured - depth) / depth
    if relative_diff > MISMATCH_THRESHOLD:
        # Do NOT commit. Record the disputed value + prior and re-ask.
        dim["value_mm"] = measured
        dim["source"] = "assumed"
        dim["confidence"] = 0.2
        dim["cross_check"] = {
            "depth_value_mm": depth,
            "ratio": round(measured / depth, 4),
            "status": "mismatch_reask",
        }
        _set_reask_question(intent, dim["name"], measured, depth)
        return

    # Within tolerance of the prior -> commit.
    _commit_measurement(dim, measured, depth, status="ok")
    _prune_reask_question(intent, dim["name"])


# ---------------------------------------------------------------------------
# Provider call with one validation-failure retry
# ---------------------------------------------------------------------------


def _call_provider_with_retry(
    photos: list[PhotoInput],
    annotation: list[dict[str, Any]] | None,
    text: str,
    catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    result = parse_intent(photos, annotation, text, catalog)
    errors = _validation_errors(result)
    if not errors:
        return result

    result = parse_intent(
        photos, annotation, text, catalog, retry_feedback="; ".join(errors)
    )
    errors = _validation_errors(result)
    if errors:
        raise HTTPException(
            status_code=502,
            detail=f"vision provider produced an invalid IntentSpec twice in a row: {'; '.join(errors)}",
        )
    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/intents")
async def create_intent(
    photos: list[UploadFile] = File(...),
    text: str = Form(...),
    annotation: str | None = Form(None),
) -> dict[str, Any]:
    if not (1 <= len(photos) <= MAX_PHOTOS):
        raise HTTPException(
            status_code=422, detail=f"Upload 1-{MAX_PHOTOS} photos, got {len(photos)}."
        )

    parsed_annotation = None
    if annotation:
        try:
            parsed_annotation = json.loads(annotation)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=422, detail=f"annotation is not valid JSON: {e}"
            )

    photo_inputs = [
        PhotoInput(
            content=await photo.read(), mime_type=photo.content_type or "image/jpeg"
        )
        for photo in photos
    ]

    try:
        result = _call_provider_with_retry(
            photo_inputs, parsed_annotation, text, _template_catalog()
        )
    except VisionProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))

    result["intent_id"] = uuid.uuid4().hex[:12]
    result = _apply_critical_dim_gate(result)
    _apply_depth_prior(result, photo_inputs)

    errors = _validation_errors(result)
    if errors:
        raise HTTPException(
            status_code=502,
            detail=f"post-processed IntentSpec failed validation: {'; '.join(errors)}",
        )

    _save_intent(result)
    return result


@router.get("/intents/{intent_id}")
def get_intent(intent_id: str) -> dict[str, Any]:
    return _load_intent(intent_id)


@router.post("/intents/{intent_id}/answers")
def submit_answers(intent_id: str, request: AnswersRequest) -> dict[str, Any]:
    intent = _load_intent(intent_id)
    questions_by_id = {q["question_id"]: q for q in intent.get("questions", [])}
    dims_by_name = {d["name"]: d for d in intent.get("dimensions", [])}

    for answer in request.answers:
        question = questions_by_id.get(answer.question_id)
        if question is None:
            raise HTTPException(
                status_code=422, detail=f"Unknown question_id '{answer.question_id}'."
            )
        _apply_answer(question, answer, dims_by_name, intent)

    intent = _apply_critical_dim_gate(intent)

    errors = _validation_errors(intent)
    if errors:
        raise HTTPException(
            status_code=500,
            detail=f"IntentSpec became invalid after answers: {'; '.join(errors)}",
        )

    _save_intent(intent)
    return intent


def _apply_answer(
    question: dict[str, Any],
    answer: AnswerInput,
    dims_by_name: dict[str, dict[str, Any]],
    intent: dict[str, Any],
) -> None:
    dim_name = question.get("dim_name")
    kind = question["kind"]

    if kind == "measure_mm":
        if answer.measure_mm is None:
            raise HTTPException(
                status_code=422,
                detail=f"question '{answer.question_id}' needs measure_mm.",
            )
        dim = dims_by_name.get(dim_name)
        if dim is None:
            raise HTTPException(
                status_code=422,
                detail=f"dimension '{dim_name}' not found on this intent.",
            )
        # Commit / flag / accept-override is decided by the cross-check, which
        # compares the value against the depth prior (CLAUDE.md rule 3).
        _cross_check_measurement(intent, dim, answer.measure_mm)
        return

    if kind == "confirm":
        # Confirming accepts the value already shown (an assumed/depth prior) as
        # the user's own. It must NOT be a back door around the cross-check
        # (CLAUDE.md rule 3): a dim currently flagged as a >20% mismatch stays
        # flagged — the user has to resolve it through the measure_mm re-ask
        # ("Yes, my measurement is right" re-submits the value), never a stray
        # confirm. For a non-flagged dim, route the shown value through the same
        # cross-check so confirming a value that disagrees with the depth prior
        # gets re-asked rather than silently stamped "ok".
        if answer.confirm and dim_name:
            dim = dims_by_name.get(dim_name)
            if (
                dim is not None
                and dim.get("value_mm") is not None
                and not _is_flagged(dim)
            ):
                _cross_check_measurement(intent, dim, dim["value_mm"])
        return

    if kind == "choice":
        # v0 scope: the only non-dimension field a choice answer can update is
        # material_suggestion. Mapping other enum template params (screw_size,
        # load_hint, etc.) from a chosen value is M5's job ("intent -> design
        # join"), once IntentSpec dims get joined with a template's full params.
        if dim_name is None and answer.choice in MATERIAL_CHOICES:
            intent["material_suggestion"] = answer.choice
        return

    # kind == "photo_retake": nothing to update server-side; the client just
    # submits a fresh POST /intents with a new photo.
