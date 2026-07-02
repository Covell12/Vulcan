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
from api.param_schema import form_fields_for
from api.vision_provider import PhotoInput, VisionProviderError, parse_intent
from templates_lib.registry import TemplateSpec, all_templates, get_template

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
INTENTS_DIR = DATA_DIR / "intents"
SCHEMA_PATH = BASE_DIR / "schemas" / "intent_spec.schema.json"

MAX_PHOTOS = 3
MATERIAL_CHOICES = ("PLA", "PETG", "TPU", "CF-PETG")


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
        dim["value_mm"] = answer.measure_mm
        dim["source"] = "user_measured"
        dim["confidence"] = 1.0
        return

    if kind == "confirm":
        if answer.confirm and dim_name:
            dim = dims_by_name.get(dim_name)
            if dim is not None:
                dim["source"] = "user_measured"
                dim["confidence"] = 1.0
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
