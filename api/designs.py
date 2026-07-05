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
from api.rendering import (
    export_design,
    heal_mesh_file,
    mesh_body_count,
    render_studio,
    write_preview_mesh,
)
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
) -> dict[str, Any]:
    """Produce STEP/STL/3MF + preview for a design. Track A templates build a
    single solid in-process; freeform (ephemeral) templates build in the sandbox
    subprocess and may return an ASSEMBLY of several parts. Both return the same
    shape: {"parts": [{name, step, stl, threemf}, ...], "preview_png": Path}."""
    if isinstance(spec, EphemeralTemplateSpec):
        return _produce_files_freeform(spec, params_obj, design_dir, callouts)

    try:
        solid = spec.build_fn(params_obj)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    exported = export_design(solid, design_dir, callouts)
    return {
        "parts": [
            {
                "name": "part",
                "step": exported["step"],
                "stl": exported["stl"],
                "threemf": exported["threemf"],
            }
        ],
        "preview_png": exported["preview_png"],
    }


def _produce_files_freeform(
    spec: EphemeralTemplateSpec,
    params_obj: BaseModel,
    design_dir: Path,
    callouts: list[dict[str, Any]],
) -> dict[str, Any]:
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

    parts = result.parts or []
    preview_path = design_dir / "preview.png"
    render_studio([p["stl"] for p in parts], preview_path, callouts)
    return {"parts": parts, "preview_png": preview_path}


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
    produced = _produce_files(spec, params_obj, design_dir, callouts)
    parts = produced["parts"]

    def _url(path: Path) -> str:
        return f"/exports/{design_id}/{path.name}"

    # Runtime printability gate (M5.5 / M9.1): EACH part we hand to a printer must
    # be a watertight, manifold, SINGLE connected body (no floating pieces — two
    # disjoint closed bodies are each watertight, so watertightness alone misses an
    # unfused add-on). A design may legitimately be an ASSEMBLY of several such
    # parts, but every part is gated individually. `heal_mesh_file` tries a light,
    # print-safe repair first so a valid solid with tessellation artifacts isn't
    # wrongly rejected. Fail closed: delete the export dir so nothing unbuildable
    # can be downloaded.
    part_urls: list[dict[str, Any]] = []
    for i, p in enumerate(parts):
        if not heal_mesh_file(p["stl"]) or mesh_body_count(p["stl"]) != 1:
            shutil.rmtree(design_dir, ignore_errors=True)
            label = p["name"] if len(parts) > 1 else "the part"
            raise HTTPException(
                status_code=500,
                detail=(
                    f"generated mesh for '{template_id}' ({label}) is not a single "
                    "watertight/manifold solid and cannot be printed; design rejected."
                ),
            )
        view = write_preview_mesh(p["stl"], out_name=f"{p['name']}_preview.stl")
        part_urls.append(
            {
                "name": p["name"],
                "step": _url(p["step"]),
                "stl": _url(p["stl"]),
                "threemf": _url(p["threemf"]),
                "view_stl": _url(view) if view is not None else None,
                "color_index": i,
            }
        )

    urls: dict[str, Any] = {
        "preview_png": _url(produced["preview_png"]),
        "parts": part_urls,
    }

    if len(parts) == 1:
        # Single part: keep the flat top-level keys existing consumers expect.
        urls["step"] = part_urls[0]["step"]
        urls["stl"] = part_urls[0]["stl"]
        urls["threemf"] = part_urls[0]["threemf"]
        urls["view_stl"] = part_urls[0]["view_stl"]
    else:
        # Assembly: merge all parts into one mesh for the in-photo composite + a
        # combined ungated view-mesh fallback. (No flat top-level STEP/3MF — those
        # are per-part downloads.)
        assembly = _write_assembly_mesh([p["stl"] for p in parts], design_dir)
        urls["stl"] = _url(assembly)
        view_all = write_preview_mesh(assembly, out_name="assembly_preview.stl")
        urls["view_stl"] = _url(view_all) if view_all is not None else None

    return design_id, urls


def _write_assembly_mesh(stl_paths: list[Path], design_dir: Path) -> Path:
    """Merge all part meshes into one `assembly.stl` (used by the in-photo
    composite and as the single-mesh 3D fallback for a multi-part design)."""
    import trimesh

    merged = trimesh.util.concatenate(
        [trimesh.load(str(p), force="mesh") for p in stl_paths]
    )
    out = design_dir / "assembly.stl"
    merged.export(str(out))
    return out


@router.post("/designs", response_model=DesignResponse)
def create_design(request: DesignRequest) -> DesignResponse:
    design_id, urls = build_design(request.template_id, request.params)
    return DesignResponse(
        design_id=design_id,
        template_id=request.template_id,
        files=DesignFiles(**urls),
    )
