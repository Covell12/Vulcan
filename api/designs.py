"""POST /designs: template_id + params JSON -> generated part + export URLs.

This is the only place template registration happens. Adding a new template
(M2) means adding one entry to TEMPLATE_REGISTRY here.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Callable

import cadquery as cq
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from api.rendering import export_design
from templates_lib.bracket_shelf_l import TEMPLATE_ID as BRACKET_SHELF_L_ID
from templates_lib.bracket_shelf_l import BracketShelfLParams, build_bracket

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
EXPORTS_DIR = BASE_DIR / "exports"

TEMPLATE_REGISTRY: dict[str, tuple[type[BaseModel], Callable[[Any], cq.Workplane]]] = {
    BRACKET_SHELF_L_ID: (BracketShelfLParams, build_bracket),
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


@router.post("/designs", response_model=DesignResponse)
def create_design(request: DesignRequest) -> DesignResponse:
    entry = TEMPLATE_REGISTRY.get(request.template_id)
    if entry is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown template_id '{request.template_id}'. Known: {sorted(TEMPLATE_REGISTRY)}",
        )
    params_model, build_fn = entry

    try:
        params = params_model(**request.params)
    except ValidationError as e:
        # str(e), not e.errors(): pydantic's ctx for custom validators can carry
        # the raw exception object, which json.dumps can't serialize.
        raise HTTPException(status_code=422, detail=str(e))

    try:
        solid = build_fn(params)
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
