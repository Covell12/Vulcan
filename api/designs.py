"""POST /designs: template_id + params JSON -> generated part + export URLs.

Templates register themselves in templates_lib.registry on import (see
templates_lib/__init__.py); adding a template in a future milestone means
writing the template module and importing it there, not touching this file.

`build_design` is the shared internals: the /designs endpoint below and the
M5 intent→design join (api/intents.py) both call it, so export logic lives in
exactly one place.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

import templates_lib  # noqa: F401  (import side effect: populates the registry)
from api.rendering import export_design
from templates_lib.registry import TemplateSpec, all_templates, get_template

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
EXPORTS_DIR = BASE_DIR / "exports"

# How a resolved param's provenance shows up on a preview callout label.
SOURCE_MARKER = {
    "measured": "✓",
    "chosen": "✓",
    "suggested": "~",
    "assumed": "~",
    "default": "",
}


class DesignRequest(BaseModel):
    template_id: str
    params: dict[str, Any]


class DesignFiles(BaseModel):
    step: str
    threemf: str
    stl: str
    preview_png: str


class DesignResponse(BaseModel):
    design_id: str
    template_id: str
    files: DesignFiles


def _callout_dicts(
    spec: TemplateSpec, params: BaseModel, source_map: dict[str, str] | None
) -> list[dict[str, Any]]:
    """Resolve each template callout to a {p0, p1, text} the renderer draws.
    The label shows the value and a source marker (measured ✓ / suggested ~ /
    default). Without a source_map (the direct /designs path), the value is
    shown plainly."""
    callouts: list[dict[str, Any]] = []
    for callout in spec.callouts_fn(params):
        value = getattr(params, callout.param, None)
        if value is None:
            continue
        marker = SOURCE_MARKER.get((source_map or {}).get(callout.param, ""), "")
        text = f"{callout.label}: {value:g} mm {marker}".rstrip()
        callouts.append({"p0": callout.p0, "p1": callout.p1, "text": text})
    return callouts


def build_design(
    template_id: str,
    params: dict[str, Any],
    *,
    source_map: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Validate params, build the solid, export STEP/3MF/STL + an annotated
    preview, and return (design_id, {file_key: url}). Raises HTTPException on an
    unknown template (400) or invalid/unbuildable params (422). This is the one
    place the export pipeline is invoked."""
    spec = get_template(template_id)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown template_id '{template_id}'. Known: {sorted(all_templates())}",
        )

    try:
        params_obj = spec.params_model(**params)
    except ValidationError as e:
        # str(e), not e.errors(): pydantic's ctx for custom validators can carry
        # the raw exception object, which json.dumps can't serialize.
        raise HTTPException(status_code=422, detail=str(e))

    try:
        solid = spec.build_fn(params_obj)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    design_id = uuid.uuid4().hex[:12]
    design_dir = EXPORTS_DIR / design_id
    callouts = _callout_dicts(spec, params_obj, source_map)
    files = export_design(solid, design_dir, callouts)

    urls = {
        "step": f"/exports/{design_id}/{files['step'].name}",
        "threemf": f"/exports/{design_id}/{files['threemf'].name}",
        "stl": f"/exports/{design_id}/{files['stl'].name}",
        "preview_png": f"/exports/{design_id}/{files['preview_png'].name}",
    }
    return design_id, urls


@router.post("/designs", response_model=DesignResponse)
def create_design(request: DesignRequest) -> DesignResponse:
    design_id, urls = build_design(request.template_id, request.params)
    return DesignResponse(
        design_id=design_id,
        template_id=request.template_id,
        files=DesignFiles(**urls),
    )
