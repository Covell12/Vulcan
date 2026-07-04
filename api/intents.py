"""POST /intents, POST /intents/{id}/answers: photos + annotation + text ->
IntentSpec, then user answers to fill in the critical dimensions.

Persistence is deliberately boring, per CLAUDE.md: one JSON file per intent
under data/intents/, no database yet. As of M5.5 the uploaded photo bytes ARE
persisted, under data/intents/<intent_id>/photos/, alongside the intent JSON.
The ghost composite preview (api/composite.py) renders the generated part back
into the user's own photo, so the photo has to survive from intent creation to
the later design join. PRIVACY: these are user-submitted photos of the user's
home/workshop and are kept on disk indefinitely with no expiry or redaction
yet; data/ is gitignored so they never reach the repo, but a real deployment
needs a retention/delete policy before this leaves the founder's own machine.

This module owns the one rule that's non-negotiable per CLAUDE.md: a
fit-critical dimension may only ever commit from source="user_measured", and
`status` can only become "ready_for_design" once every critical dimension for
the chosen template has been. That gate is computed here, in code, every
time the IntentSpec changes — never trusted from the vision provider's own
opinion of it (see `_apply_critical_dim_gate`).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any

import jsonschema
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

import templates_lib  # noqa: F401  (import side effect: populates the registry)
from api import codegen_provider, composite, design_store, freeform
from api.codegen_provider import CodegenProviderError
from api.depth_provider import (
    DepthProviderError,
    ScaleRegion,
    depth_mm_at,
    estimate_scale,
)
from api.designs import EXPORTS_DIR, build_design
from api.param_schema import form_fields_for
from api.photo import PhotoInput
from api.vision_provider import VisionProviderError, parse_intent
from templates_lib.registry import TemplateSpec, all_templates, get_template

logger = logging.getLogger(__name__)

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


# Uploaded photos are persisted so the design join can render the ghost
# composite (api/composite.py) into the user's original photo. See the module
# docstring for the privacy implication.
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
}


def _intent_dir(intent_id: str) -> Path:
    return INTENTS_DIR / intent_id


def _persist_photos(
    intent_id: str, photo_inputs: list[PhotoInput]
) -> list[dict[str, Any]]:
    """Write each uploaded photo to data/intents/<id>/photos/ and return a list
    of {index, path (relative to data/), mime_type} refs to store on the intent.
    The relative path lets the join reload the exact bytes later."""
    photos_dir = _intent_dir(intent_id) / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    refs: list[dict[str, Any]] = []
    for i, photo in enumerate(photo_inputs):
        ext = _MIME_EXT.get((photo.mime_type or "").lower(), ".jpg")
        dest = photos_dir / f"photo_{i}{ext}"
        dest.write_bytes(photo.content)
        refs.append(
            {
                "index": i,
                "path": str(dest.relative_to(DATA_DIR)),
                "mime_type": photo.mime_type,
            }
        )
    return refs


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


def _is_confirmed(dim: dict[str, Any] | None) -> bool:
    """A critical dim counts as confirmed only when it's user_measured AND
    actually carries a value — source alone isn't enough (a provider could send
    source="user_measured" with value_mm=null, which must NOT open the gate)."""
    return bool(
        dim and dim.get("source") == "user_measured" and dim.get("value_mm") is not None
    )


def _apply_critical_dim_gate(intent: dict[str, Any]) -> dict[str, Any]:
    template_id = intent.get("template_id")
    spec = get_template(template_id) if template_id else None

    # Runs first, template or not: keep the intent internally consistent so a
    # measure_mm question can always be answered (see the function's docstring).
    _ensure_measure_question_dimensions(intent)

    if spec is not None:
        _ensure_critical_dimensions(intent, spec)
        _ensure_critical_questions(intent, spec)
        _attach_param_bounds(intent, spec)

    if intent.get("out_of_scope_reason"):
        intent["status"] = "out_of_scope"
    elif spec is not None:
        dims_by_name = {d["name"]: d for d in intent["dimensions"]}
        all_confirmed = all(
            _is_confirmed(dims_by_name.get(name)) for name in spec.critical_dims
        )
        intent["status"] = "ready_for_design" if all_confirmed else "needs_answers"
    else:
        intent["status"] = "needs_answers"

    return intent


def _derive_dim_name(question_id: str | None) -> str:
    """A snake_case, `_mm`-suffixed dimension name derived from a question id, for
    a measure_mm question the provider left with no dim_name (e.g. an invented
    "q_wall_to_faucet_center" → "wall_to_faucet_center_mm"). Deterministic and
    never empty, so the answer always has a dimension to land in."""
    base = (
        re.sub(r"[^0-9a-zA-Z]+", "_", question_id or "measurement").strip("_").lower()
    )
    for prefix in ("reask_", "auto_", "question_", "q_"):
        if base.startswith(prefix):
            base = base[len(prefix) :]
            break
    base = base.strip("_") or "measurement"
    return base if base.endswith("_mm") else f"{base}_mm"


def _attach_param_bounds(intent: dict[str, Any], spec: TemplateSpec) -> None:
    """Expose each numeric param's min/max (mm) so the UI can show the allowed
    range on the measurement field and reject an out-of-range value BEFORE the
    join — otherwise a value outside the template's pydantic bounds (common for a
    freeform template whose generated ranges are tighter than the real part)
    only fails at build time with a confusing 422."""
    bounds: dict[str, dict[str, Any]] = {}
    for field in form_fields_for(spec.params_model):
        if field["type"] in ("number", "integer") and (
            field.get("minimum") is not None or field.get("maximum") is not None
        ):
            bounds[field["name"]] = {
                "minimum": field.get("minimum"),
                "maximum": field.get("maximum"),
            }
    intent["param_bounds"] = bounds


def _ensure_measure_question_dimensions(intent: dict[str, Any]) -> None:
    """Every measure_mm question implies a dimension to hold its answer. Vision
    providers sometimes ask the user to measure something without a usable
    `dim_name` — either a name that's absent from `dimensions[]` (e.g.
    "gap_wall_to_tap_mm") or no dim_name at all (an invented extra measurement
    like "q_wall_to_faucet_center") — both of which made answering that question
    422. Give every measure_mm question a dim_name (deriving one from its id when
    missing) and a matching dimension — non-critical by default; the critical-dim
    gate that runs right after is the only thing that promotes a dim to critical,
    and only for the template's real critical_dims. Runs at intent creation AND
    after every answer, so the stored intent is always self-consistent."""
    questions = intent.get("questions") or []
    dims_by_name = {d["name"]: d for d in intent.setdefault("dimensions", [])}
    for q in questions:
        if q.get("kind") != "measure_mm":
            continue
        name = q.get("dim_name")
        if not name:
            name = _derive_dim_name(q.get("question_id"))
            q["dim_name"] = name  # persist so the UI + answer path both see it
        if name in dims_by_name:
            continue
        dim = {
            "name": name,
            "value_mm": None,
            "source": "assumed",
            "confidence": 0.0,
            "critical": False,
            "cross_check": None,
        }
        intent["dimensions"].append(dim)
        dims_by_name[name] = dim


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

    # Persist the photos (for the later ghost composite) + the raw annotation
    # (for placing the ghost at the user's circled spot). Both are referenced by
    # the design join; neither is part of the canonical IntentSpec schema, which
    # allows extra top-level fields.
    result["photos"] = _persist_photos(result["intent_id"], photo_inputs)
    result["annotation"] = parsed_annotation
    # Keep the raw request text for the freeform code generator (the LLM
    # restatement in `description` loses detail we want when authoring a template).
    result["request_text"] = text
    _apply_freeform_routing(result)

    errors = _validation_errors(result)
    if errors:
        raise HTTPException(
            status_code=502,
            detail=f"post-processed IntentSpec failed validation: {'; '.join(errors)}",
        )

    _save_intent(result)
    return result


# Below this template_fit, the matched template is treated as a poor fit and
# freeform is recommended alongside it (Part A). Also recommended whenever the
# provider listed unsupported_features it can't express, or no template matched.
FREEFORM_FIT_THRESHOLD = 0.65


def _apply_freeform_routing(intent: dict[str, Any]) -> None:
    """Decide whether to offer / recommend the freeform (custom-design) path.

    The intent router used to be greedy: it always rode a matched template even
    when the template couldn't really make the part. Now the provider reports
    `template_fit` (0-1) and `unsupported_features`; a poor fit (< threshold) or
    ANY unsupported feature, or no template at all, makes freeform recommended.
    freeform is AVAILABLE for any in-scope request (the UI's always-on "Design
    this custom instead" override), and RECOMMENDED when the template is a bad
    match. Sets fields the UI reads; never blocks the template path."""
    in_scope = intent.get("status") != "out_of_scope"
    has_template = bool(intent.get("template_id"))
    fit = intent.get("template_fit")
    unsupported = intent.get("unsupported_features") or []
    poor_fit = (isinstance(fit, (int, float)) and fit < FREEFORM_FIT_THRESHOLD) or bool(
        unsupported
    )

    intent["freeform_available"] = in_scope
    intent["freeform_recommended"] = in_scope and (not has_template or poor_fit)


def _load_intent_photos(intent: dict[str, Any]) -> list[PhotoInput]:
    """Reload the persisted photos (M5.5) so the freeform generator can see what
    the user photographed. Returns [] if none were stored / are missing."""
    photos: list[PhotoInput] = []
    for ref in intent.get("photos") or []:
        path = DATA_DIR / ref.get("path", "")
        if path.exists():
            photos.append(
                PhotoInput(
                    content=path.read_bytes(),
                    mime_type=ref.get("mime_type") or "image/jpeg",
                )
            )
    return photos


def _freeform_questions(outcome: "freeform.GenerationResult") -> list[dict[str, Any]]:
    """One measure_mm question per generated critical dim, carrying the overlay
    the codegen model placed on the photo (or null if it couldn't locate it)."""
    questions: list[dict[str, Any]] = []
    for dim in outcome.critical_dims:
        pretty = dim.removesuffix("_mm").replace("_", " ")
        questions.append(
            {
                "question_id": f"gen-{dim}",
                "dim_name": dim,
                "prompt": f"What is the {pretty}, in mm?",
                "kind": "measure_mm",
                "choices": None,
                "overlay": outcome.overlays.get(dim),
                "suggested_value": None,
                "chosen_value": None,
            }
        )
    return questions


@router.post("/intents/{intent_id}/freeform")
def start_freeform(intent_id: str) -> dict[str, Any]:
    """Track B: author a one-off template and attach it to the intent so the
    standard questions → measure → join → review flow takes over. Works as the
    always-available user OVERRIDE too — a template may already be matched (Part
    A: "Design this custom instead"), in which case the generated template
    REPLACES it. On failure the request is logged to the demand log and an honest
    error is returned on the intent."""
    intent = _load_intent(intent_id)
    if intent.get("status") == "out_of_scope":
        raise HTTPException(
            status_code=409,
            detail=f"intent '{intent_id}' is out of scope: {intent.get('out_of_scope_reason')}",
        )

    # Clear, up-front error if the codegen provider isn't configured, rather than
    # burning the whole self-repair budget on auth failures.
    try:
        codegen_provider.check_provider_configured()
    except CodegenProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))

    dims_hints = [
        {"name": d["name"], "value_mm": d.get("value_mm")}
        for d in intent.get("dimensions", [])
        if d.get("value_mm") is not None
    ]
    request_text = intent.get("request_text") or intent.get("description") or ""

    outcome = freeform.generate_and_register(
        request_text, _load_intent_photos(intent), dims_hints
    )

    if not outcome.ok:
        intent["freeform_error"] = outcome.error
        intent["freeform_available"] = False  # tried and failed; logged to demand
        _save_intent(intent)
        return intent

    # Success: adopt the generated template. If this is an OVERRIDE of a matched
    # template, drop that template's dimensions/questions — they measured a
    # different part. Seed the questions for the generated critical dims WITH the
    # overlays the codegen model placed on the photo, so the photo shows the
    # dimension drawing (freeform questions used to be synthesized overlay-less).
    intent["template_id"] = outcome.template_id
    intent["freeform"] = True
    intent["freeform_assumptions"] = outcome.assumptions
    intent["freeform_dfm"] = outcome.dfm
    intent["dimensions"] = []
    intent["questions"] = _freeform_questions(outcome)
    intent.pop("freeform_error", None)
    intent = _apply_critical_dim_gate(intent)
    _apply_freeform_routing(intent)

    errors = _validation_errors(intent)
    if errors:
        raise HTTPException(
            status_code=500,
            detail=f"IntentSpec became invalid after freeform generation: {'; '.join(errors)}",
        )
    _save_intent(intent)
    return intent


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


# ---------------------------------------------------------------------------
# The intent -> design join (M5)
# ---------------------------------------------------------------------------

# How a dimension's source maps to the join's provenance label (which drives
# the preview marker and the summary table).
_DIM_SOURCE_LABEL = {
    "user_measured": "measured",
    "depth_inferred": "suggested",
    "assumed": "assumed",
}


def _coerce_param(value: Any, field_type: str) -> Any:
    """Coerce a resolved enum/bool value (which may arrive as a string from a
    choice question) to the type the template's params model expects."""
    if field_type == "boolean" and isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return value


def _resolve_design_params(
    intent: dict[str, Any], spec: TemplateSpec
) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    """Map the IntentSpec onto the template's FULL param set (M5 join).

    Precedence:
      - dimension params: the dimension's value_mm, tagged by its source.
        A critical param may only come from user_measured (the gate guarantees
        this at ready_for_design; we double-check and 409 if it's been violated).
      - enum/boolean params: an answered choice question (chosen_value) beats the
        provider's suggested_value beats the template default.
      - every other (non-dimension numeric) param: the template default.

    Returns (params dict for the model, source_map for the preview, summary rows).
    """
    fields = form_fields_for(spec.params_model)
    dims_by_name = {d["name"]: d for d in intent.get("dimensions", [])}

    chosen: dict[str, str] = {}
    suggested: dict[str, str] = {}
    for q in intent.get("questions", []):
        if q.get("kind") != "choice":
            continue
        param = q.get("dim_name")
        if not param:
            continue
        if q.get("chosen_value") is not None:
            chosen[param] = q["chosen_value"]
        if q.get("suggested_value") is not None:
            suggested[param] = q["suggested_value"]

    params: dict[str, Any] = {}
    source_map: dict[str, str] = {}
    summary: list[dict[str, Any]] = []

    for field in fields:
        name = field["name"]
        ftype = field["type"]
        dim = dims_by_name.get(name)

        # A critical dim must be a genuine confirmed measurement — checked FIRST
        # so a critical dim that's missing, valueless, or not user_measured can
        # never fall through to a template default (CLAUDE.md rule 2). This is a
        # defensive backstop; the ready_for_design gate should already guarantee
        # it, but the join never trusts that.
        if name in spec.critical_dims:
            if not _is_confirmed(dim):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"critical dimension '{name}' has not been measured and "
                        "confirmed by the user — a part cannot be made without it."
                    ),
                )
            value, source = dim["value_mm"], "measured"
        elif dim is not None and dim.get("value_mm") is not None:
            value = dim["value_mm"]
            source = _DIM_SOURCE_LABEL.get(dim.get("source"), "assumed")
        elif ftype in ("choice", "boolean"):
            if name in chosen:
                value, source = _coerce_param(chosen[name], ftype), "chosen"
            elif name in suggested:
                value, source = _coerce_param(suggested[name], ftype), "suggested"
            else:
                value, source = field["default"], "default"
        else:
            value, source = field["default"], "default"

        params[name] = value
        source_map[name] = source
        summary.append(
            {
                "name": name,
                "label": field["label"],
                "value": value,
                "source": source,
                "unit": "mm" if name.endswith("_mm") else "",
            }
        )

    return params, source_map, summary


# Hard wall-clock cap on the ghost's optional depth lookup. With
# DEPTH_PROVIDER=replicate that lookup is a synchronous network call inside the
# design request; the ghost is only a preview, so it must never make the join
# hang once the part files are already built. Under the default
# DEPTH_PROVIDER=none, depth_mm_at returns None instantly and this never waits.
_COMPOSITE_DEPTH_DEADLINE_S = 6.0


def _bounded_depth_mm_at(
    photo: PhotoInput,
    x: float,
    y: float,
    deadline_s: float = _COMPOSITE_DEPTH_DEADLINE_S,
) -> float | None:
    """`depth_mm_at` with a hard deadline. On timeout returns None (fall back to
    non-depth scale); the orphaned worker finishes in the background and its
    result is discarded — we never block the join on it."""
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        return executor.submit(depth_mm_at, photo, x, y).result(timeout=deadline_s)
    except FuturesTimeout:
        return None
    finally:
        # Don't wait=True: a stalled depth call must not extend the request.
        executor.shutdown(wait=False)


def _render_ghost_composite(
    intent: dict[str, Any], spec: TemplateSpec, design_id: str
) -> str | None:
    """Best-effort ghost composite (M5.5): render the just-built part into the
    user's stored photo and return its /exports URL. Returns None — never
    raises — when there's no stored photo or anything goes wrong; the part
    files are the real deliverable and a preview must never block them."""
    photos = intent.get("photos") or []
    if not photos:
        return None
    photo_ref = photos[0]
    photo_path = DATA_DIR / photo_ref.get("path", "")
    if not photo_path.exists():
        return None

    try:
        photo_bytes = photo_path.read_bytes()
        annotation = intent.get("annotation")

        # Optional: true metric depth at the circled point (usually unavailable
        # with DEPTH_PROVIDER=none; render_composite then infers scale itself).
        # Deadline-bounded so a slow replicate backend can't hang the join.
        centroid = composite.annotation_centroid(annotation) or (0.5, 0.5)
        depth_mm = _bounded_depth_mm_at(
            PhotoInput(
                content=photo_bytes,
                mime_type=photo_ref.get("mime_type") or "image/jpeg",
            ),
            centroid[0],
            centroid[1],
        )

        out_path = EXPORTS_DIR / design_id / "composite.png"
        composite.render_composite(
            photo_bytes,
            EXPORTS_DIR / design_id / "part.stl",
            out_path,
            category=spec.category,
            annotation=annotation,
            depth_mm=depth_mm,
        )
        return f"/exports/{design_id}/{out_path.name}"
    except Exception as e:  # noqa: BLE001 — preview is strictly best-effort
        logger.warning("ghost composite failed for design %s: %s", design_id, e)
        return None


@router.post("/intents/{intent_id}/design")
def create_design_from_intent(intent_id: str) -> dict[str, Any]:
    intent = _load_intent(intent_id)

    if intent.get("status") != "ready_for_design":
        raise HTTPException(
            status_code=409,
            detail=(
                f"intent '{intent_id}' is '{intent.get('status')}', not ready_for_design "
                "— every critical dimension must be measured and confirmed first."
            ),
        )

    template_id = intent.get("template_id")
    spec = get_template(template_id) if template_id else None
    if spec is None:
        raise HTTPException(
            status_code=409,
            detail=f"intent '{intent_id}' has no buildable template_id.",
        )

    params, source_map, summary = _resolve_design_params(intent, spec)
    design_id, files = build_design(template_id, params, source_map=source_map)

    # In-photo ghost preview (only when a photo was stored at intent creation).
    composite_url = _render_ghost_composite(intent, spec, design_id)
    if composite_url:
        files["composite"] = composite_url

    # Freeform (Track B): every generated design lands in the founder review
    # queue, and its CAD downloads are gated until approved (see api/review).
    is_freeform = bool(intent.get("freeform"))
    review_status = None
    if is_freeform:
        review_status = design_store.STATUS_PENDING
        design_store.save_record(
            {
                "design_id": design_id,
                "intent_id": intent_id,
                "is_freeform": True,
                "status": review_status,
                "template_id": template_id,
                "request": intent.get("request_text") or intent.get("description"),
                "code": getattr(spec, "code", ""),
                "params": summary,
                "assumptions": intent.get("freeform_assumptions", []),
                "dfm": intent.get("freeform_dfm"),
                "files": files,
                "created_at": freeform._now(),
            }
        )

    # Persist the intent -> design link.
    intent["design_id"] = design_id
    intent["design_files"] = files
    intent["design_params"] = summary
    _save_intent(intent)

    response: dict[str, Any] = {
        "design_id": design_id,
        "template_id": template_id,
        "files": files,
        "params": summary,
    }
    if is_freeform:
        response["freeform"] = True
        response["review_status"] = review_status
        response["assumptions"] = intent.get("freeform_assumptions", [])
    return response


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
        # A measure_mm question always has SOMETHING to measure: use its
        # dim_name, or derive one from the question id when the provider left it
        # null (normally the gate does this at creation, but an older stored
        # intent may predate that). Either way answering it never 422s.
        if not dim_name:
            dim_name = _derive_dim_name(question.get("question_id"))
            question["dim_name"] = dim_name
        dim = dims_by_name.get(dim_name)
        if dim is None:
            # A measure_mm question whose dimension the provider forgot to list
            # (normally synthesized by the gate at intent creation, but an
            # older/stored intent may predate that). Create it on demand rather
            # than dead-end the user with a 422.
            dim = {
                "name": dim_name,
                "value_mm": None,
                "source": "assumed",
                "confidence": 0.0,
                "critical": False,
                "cross_check": None,
            }
            intent.setdefault("dimensions", []).append(dim)
            dims_by_name[dim_name] = dim
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
        if answer.choice is not None:
            # Record the chosen enum value ON the question, so the design join
            # (POST /intents/{id}/design) can resolve it — a choice question's
            # dim_name is the enum template param it sets (e.g. "load_hint").
            # This is the M3 deferral finished: choices now map to ANY enum
            # param, not just material.
            question["chosen_value"] = answer.choice
            # material_suggestion is intent metadata (not a template param), so
            # it's still updated directly when its question is answered.
            if dim_name == "material_suggestion" or (
                dim_name is None and answer.choice in MATERIAL_CHOICES
            ):
                intent["material_suggestion"] = answer.choice
        return

    # kind == "photo_retake": nothing to update server-side; the client just
    # submits a fresh POST /intents with a new photo.
