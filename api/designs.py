"""POST /designs: template_id + params JSON -> generated part + export URLs.

Templates register themselves in templates_lib.registry on import (see
templates_lib/__init__.py); adding a template in a future milestone means
writing the template module and importing it there, not touching this file.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

import templates_lib  # noqa: F401  (import side effect: populates the registry)
from api.rendering import export_design
from templates_lib.registry import all_templates, get_template

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
EXPORTS_DIR = BASE_DIR / "exports"


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


@router.post("/designs", response_model=DesignResponse)
def create_design(request: DesignRequest) -> DesignResponse:
    spec = get_template(request.template_id)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown template_id '{request.template_id}'. Known: {sorted(all_templates())}",
        )

    try:
        params = spec.params_model(**request.params)
    except ValidationError as e:
        # str(e), not e.errors(): pydantic's ctx for custom validators can carry
        # the raw exception object, which json.dumps can't serialize.
        raise HTTPException(status_code=422, detail=str(e))

    try:
        solid = spec.build_fn(params)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    design_id = uuid.uuid4().hex[:12]
    design_dir = EXPORTS_DIR / design_id
    files = export_design(solid, design_dir)

    return DesignResponse(
        design_id=design_id,
        template_id=request.template_id,
        files=DesignFiles(
            step=f"/exports/{design_id}/{files['step'].name}",
            threemf=f"/exports/{design_id}/{files['threemf'].name}",
            stl=f"/exports/{design_id}/{files['stl'].name}",
            preview_png=f"/exports/{design_id}/{files['preview_png'].name}",
        ),
    )
