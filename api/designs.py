"""POST /designs: template_id + params JSON -> generated part + export URLs.

Templates register themselves in templates_lib.registry on import (see
templates_lib/__init__.py); adding a template in a future milestone means
writing the template module and importing it there, not touching this file.

`build_design` is the shared internals: the /designs endpoint below and the
M5 intent→design join (api/intents.py) both call it, so export logic lives in
exactly one place.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

import templates_lib  # noqa: F401  (import side effect: populates the registry)
from api.rendering import export_design, heal_mesh_file, render_preview
from templates_lib.registry import (
    EphemeralTemplateSpec,
    TemplateSpec,
    all_templates,
    get_template,
)

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


def _produce_files(
    spec: TemplateSpec,
    params_obj: BaseModel,
    design_dir: Path,
    callouts: list[dict[str, Any]],
) -> dict[str, Path]:
    """Produce STEP/STL/3MF + preview for a design. Track A templates build a
    solid in-process and export it; freeform (ephemeral) templates build in the
    sandbox subprocess (their generated code never runs here) and the preview is
    rendered in-process from the sandbox's STL. Both return the same file dict,
    so the manifold gate and URL construction downstream are identical."""
    if isinstance(spec, EphemeralTemplateSpec):
        return _produce_files_freeform(spec, params_obj, design_dir, callouts)

    try:
        solid = spec.build_fn(params_obj)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return export_design(solid, design_dir, callouts)


def _produce_files_freeform(
    spec: EphemeralTemplateSpec,
    params_obj: BaseModel,
    design_dir: Path,
    callouts: list[dict[str, Any]],
) -> dict[str, Path]:
    # Local import so the (heavy, subprocess-spawning) sandbox is only loaded on
    # the freeform path, and to avoid any import cycle.
    from api import sandbox

    result = sandbox.run_generated_build(spec.code, params_obj.model_dump(), design_dir)
    if not result.ok:
        shutil.rmtree(design_dir, ignore_errors=True)
        # A verify-stage failure would mean the stored code became unsafe — that's
        # an internal problem (500). A run/timeout/output failure means these
        # particular user params produced a bad build (422).
        status = 422 if result.stage in ("run", "timeout", "output") else 500
        raise HTTPException(
            status_code=status,
            detail=f"freeform build failed ({result.stage}): {result.error}",
        )

    preview_path = design_dir / "preview.png"
    render_preview(result.files["stl"], preview_path, callouts)
    return {
        "step": result.files["step"],
        "stl": result.files["stl"],
        "threemf": result.files["threemf"],
        "preview_png": preview_path,
    }


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

    design_id = uuid.uuid4().hex[:12]
    design_dir = EXPORTS_DIR / design_id
    callouts = _callout_dicts(spec, params_obj, source_map)
    files = _produce_files(spec, params_obj, design_dir, callouts)

    # Runtime manifold gate (M5.5): a part we hand to a printer must be a
    # watertight, manifold solid. The per-template pytest suite proves this for
    # DEFAULT params, but a live design runs USER-resolved params (and generated
    # geometry), so we re-check the actually-exported mesh here and refuse to
    # return files for a non-printable design. `heal_mesh_file` first tries a
    # light, print-safe repair (and re-exports the healed STL) so a valid solid
    # with tessellation artifacts isn't wrongly rejected; a genuinely broken mesh
    # still fails. Fail closed: delete the half-baked export directory so no
    # unbuildable STL/STEP can be downloaded.
    if not heal_mesh_file(files["stl"]):
        shutil.rmtree(design_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail=(
                f"generated mesh for template '{template_id}' is not watertight/"
                "manifold and cannot be printed; design rejected."
            ),
        )

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
