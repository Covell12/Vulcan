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

# Size ceiling for the shipped-artifact gate (mirrors api.freeform.MAX_BBOX_MM;
# kept as a local literal so designs.py has no freeform import at module load).
_MAX_BBOX_MM = 250.0

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
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Validate params, build the solid, export STEP/3MF/STL + an annotated
    preview, and return (design_id, {file_key: url}, shipped_dfm). Raises
    HTTPException on an unknown template (400), invalid/unbuildable params (422),
    or a mesh that fails the printability gate (500). This is the one place the
    export pipeline is invoked.

    `shipped_dfm` measures the ACTUAL final artifact we hand off — the exported
    `part.stl` for a single part, or the merged `assembly.stl` for an assembly —
    so a design record's DFM describes what SHIPS, not a generation-time build
    with different (default) params."""
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
        shipped_stl = parts[0]["stl"]
    else:
        # Assembly: merge all parts into one mesh for the in-photo composite + a
        # combined ungated view-mesh fallback. (No flat top-level STEP/3MF — those
        # are per-part downloads.)
        assembly = _write_assembly_mesh([p["stl"] for p in parts], design_dir)
        urls["stl"] = _url(assembly)
        view_all = write_preview_mesh(assembly, out_name="assembly_preview.stl")
        urls["view_stl"] = _url(view_all) if view_all is not None else None
        shipped_stl = assembly

    # Re-gate + measure the FINAL shipped artifact (the exact bytes we hand off),
    # not the per-part temp meshes. An assembly's merged STL legitimately has one
    # connected body per part, so we expect exactly `len(parts)` bodies here — a
    # different count means the merge/export diverged from the gated parts.
    shipped_dfm = _measure_and_gate_shipped(
        shipped_stl,
        expected_bodies=len(parts),
        template_id=template_id,
        design_dir=design_dir,
    )
    return design_id, urls, shipped_dfm


def _measure_and_gate_shipped(
    stl_path: Path,
    *,
    expected_bodies: int,
    template_id: str,
    design_dir: Path,
) -> dict[str, Any]:
    """Measure the final shipped mesh and gate it: watertight, exactly
    `expected_bodies` connected bodies (1 for a single part, one-per-part for an
    assembly), and within the size ceiling. Returns the DFM report describing what
    ACTUALLY ships. Fail closed (delete the export dir, raise 500) on divergence —
    this catches a build whose user-params geometry split, grew past the ceiling,
    or whose assembly merge produced an unexpected body count."""
    import trimesh

    mesh = trimesh.load(str(stl_path), force="mesh")
    faces = getattr(mesh, "faces", [])
    watertight = bool(getattr(mesh, "is_watertight", False)) and len(faces) > 0
    bodies = mesh_body_count(str(stl_path))
    bounds = getattr(mesh, "bounds", None)
    extent = (
        [float(bounds[1][i] - bounds[0][i]) for i in range(3)]
        if bounds is not None
        else [0.0, 0.0, 0.0]
    )
    max_extent = max(extent) if extent else 0.0
    within = 0 < max_extent <= _MAX_BBOX_MM + 1e-6
    connected = bodies == expected_bodies
    dfm: dict[str, Any] = {
        "manifold": watertight,
        "connected": connected,
        "body_count": bodies,
        "expected_bodies": expected_bodies,
        "part_count": expected_bodies,
        "within_size": within,
        "max_extent_mm": round(max_extent, 2),
        "bbox_mm": [round(x, 2) for x in extent],
        "size_ceiling_mm": _MAX_BBOX_MM,
        "measured_on": "shipped_artifact",
    }
    if not (watertight and connected and within):
        shutil.rmtree(design_dir, ignore_errors=True)
        problems = []
        if not watertight:
            problems.append("not watertight/manifold")
        if not connected:
            problems.append(f"{bodies} bodies (expected {expected_bodies})")
        if not within:
            problems.append(
                f"largest dim {dfm['max_extent_mm']}mm over {_MAX_BBOX_MM}mm"
            )
        raise HTTPException(
            status_code=500,
            detail=(
                f"shipped mesh for '{template_id}' failed the final printability gate "
                f"({'; '.join(problems)}); design rejected."
            ),
        )
    return dfm


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
    # Track A (registry template) designs aren't recorded/reviewed, so the shipped
    # DFM is only consumed as the printability gate inside build_design here.
    design_id, urls, _dfm = build_design(request.template_id, request.params)
    return DesignResponse(
        design_id=design_id,
        template_id=request.template_id,
        files=DesignFiles(**urls),
    )
