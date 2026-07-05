"""Export a CadQuery solid to STEP/3MF/STL and render a PNG preview.

Preview rendering uses matplotlib (Agg backend) over the exported STL's
triangle mesh rather than a CAD viewer, so it works headless with no display
or GPU — important since this runs inside the API process on a server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cadquery as cq
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import trimesh  # noqa: E402
from cadquery import exporters  # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: E402

FACE_COLOR = (0.70, 0.75, 0.85, 1.0)
EDGE_COLOR = (0.2, 0.2, 0.2, 0.3)
CALLOUT_COLOR = (0.83, 0.25, 0.10)


def export_design(
    solid: cq.Workplane,
    design_dir: Path,
    callouts: list[dict[str, Any]] | None = None,
) -> dict[str, Path]:
    """Write STEP/3MF/STL + a PNG preview for `solid` into design_dir. When
    `callouts` are given (each: {"p0", "p1", "text"}), the preview is annotated
    with labeled dimension arrows. Returns file paths."""
    design_dir.mkdir(parents=True, exist_ok=True)

    step_path = design_dir / "part.step"
    threemf_path = design_dir / "part.3mf"
    stl_path = design_dir / "part.stl"
    preview_path = design_dir / "preview.png"

    exporters.export(solid, str(step_path))
    exporters.export(solid, str(stl_path))
    exporters.export(solid, str(threemf_path))

    render_preview(stl_path, preview_path, callouts)

    return {
        "step": step_path,
        "threemf": threemf_path,
        "stl": stl_path,
        "preview_png": preview_path,
    }


def render_preview(
    stl_path: Path,
    preview_path: Path,
    callouts: list[dict[str, Any]] | None = None,
) -> None:
    mesh = trimesh.load(str(stl_path))
    fig = plt.figure(figsize=(6, 6))
    # try/finally so the figure is ALWAYS closed — a leaked figure lingers in
    # matplotlib's global registry inside this long-lived server process, so an
    # exception on the savefig / draw path (e.g. a read-only exports dir) would
    # otherwise accumulate figures across requests.
    try:
        ax = fig.add_subplot(111, projection="3d")
        poly = Poly3DCollection(
            mesh.vertices[mesh.faces],
            facecolor=FACE_COLOR,
            edgecolor=EDGE_COLOR,
            linewidths=0.3,
        )
        ax.add_collection3d(poly)
        # Pad any zero-thickness axis: a degenerate/flat mesh (e.g. a broken
        # template producing a single planar face) otherwise makes matplotlib's
        # 3D projection singular and crashes. We still render it — the runtime
        # manifold gate downstream (api/designs.build_design) is what rejects
        # such a part, and it should get a clean rejection, not a traceback from
        # the preview step.
        lo, hi = mesh.bounds[0].copy(), mesh.bounds[1].copy()
        for i in range(3):
            if hi[i] - lo[i] <= 1e-9:
                lo[i] -= 1.0
                hi[i] += 1.0
        bounds = [lo, hi]
        ax.set_xlim(lo[0], hi[0])
        ax.set_ylim(lo[1], hi[1])
        ax.set_zlim(lo[2], hi[2])
        ax.set_box_aspect(hi - lo)
        ax.view_init(elev=25, azim=-60)
        ax.axis("off")

        if callouts:
            _draw_callouts(ax, callouts, bounds)

        fig.savefig(str(preview_path), dpi=120, bbox_inches="tight")
    finally:
        plt.close(fig)


# Distinct matplotlib RGBA colors per part in an assembly preview. Kept roughly
# in step with the client palette in web/viewer3d.js so the PNG and the 3D
# viewer tell the same "which piece is which" story (ember first, then blue,
# green, gold, purple, teal, pink).
PART_PALETTE = [
    (0.98, 0.42, 0.11, 1.0),
    (0.29, 0.62, 0.94, 1.0),
    (0.42, 0.80, 0.36, 1.0),
    (0.95, 0.78, 0.25, 1.0),
    (0.72, 0.45, 0.92, 1.0),
    (0.25, 0.80, 0.78, 1.0),
    (0.95, 0.45, 0.70, 1.0),
    (0.60, 0.65, 0.72, 1.0),
]


def render_assembly_preview(
    stl_paths: list[Path],
    preview_path: Path,
    callouts: list[dict[str, Any]] | None = None,
) -> None:
    """Render an assembly of parts into one PNG, each part a distinct color, so
    the founder/customer can see how the pieces fit together. Falls back to the
    single-part renderer when there's exactly one part."""
    if len(stl_paths) <= 1:
        render_preview(stl_paths[0], preview_path, callouts)
        return

    import numpy as np

    meshes = [trimesh.load(str(p), force="mesh") for p in stl_paths]
    fig = plt.figure(figsize=(6, 6))
    try:
        ax = fig.add_subplot(111, projection="3d")
        for i, mesh in enumerate(meshes):
            color = PART_PALETTE[i % len(PART_PALETTE)]
            ax.add_collection3d(
                Poly3DCollection(
                    mesh.vertices[mesh.faces],
                    facecolor=color,
                    edgecolor=(0.1, 0.1, 0.1, 0.25),
                    linewidths=0.2,
                )
            )
        allv = np.vstack([m.vertices for m in meshes])
        lo, hi = allv.min(axis=0), allv.max(axis=0)
        for i in range(3):
            if hi[i] - lo[i] <= 1e-9:
                lo[i] -= 1.0
                hi[i] += 1.0
        ax.set_xlim(lo[0], hi[0])
        ax.set_ylim(lo[1], hi[1])
        ax.set_zlim(lo[2], hi[2])
        ax.set_box_aspect(hi - lo)
        ax.view_init(elev=25, azim=-60)
        ax.axis("off")
        if callouts:
            _draw_callouts(ax, callouts, [lo, hi])
        fig.savefig(str(preview_path), dpi=120, bbox_inches="tight")
    finally:
        plt.close(fig)


# Four canonical camera angles (elev, azim) for the visual-critique renders — an
# isometric plus front/side/top, so the critique model can judge the shape from
# every side (a defect hidden in one view shows in another).
CANONICAL_VIEWS = [
    ("iso", 25, -60),
    ("front", 8, -90),
    ("side", 8, 0),
    ("top", 89, -90),
]


def render_canonical_views(
    stl_paths: list[Path], out_dir: Path, prefix: str = "view"
) -> list[Path]:
    """Render the part (or colored assembly) from the 4 CANONICAL_VIEWS into
    out_dir as `<prefix>_<name>.png`, and return the paths. Used by the freeform
    visual-critique loop to give the vision model 'eyes' on what it built. Reuses
    the same matplotlib/trimesh path as the previews (headless, no GPU)."""
    import numpy as np

    out_dir.mkdir(parents=True, exist_ok=True)
    meshes = [trimesh.load(str(p), force="mesh") for p in stl_paths]
    allv = np.vstack([m.vertices for m in meshes])
    lo, hi = allv.min(axis=0), allv.max(axis=0)
    for i in range(3):
        if hi[i] - lo[i] <= 1e-9:
            lo[i] -= 1.0
            hi[i] += 1.0

    paths: list[Path] = []
    for name, elev, azim in CANONICAL_VIEWS:
        fig = plt.figure(figsize=(5, 5))
        try:
            ax = fig.add_subplot(111, projection="3d")
            for j, mesh in enumerate(meshes):
                color = PART_PALETTE[j % len(PART_PALETTE)]
                ax.add_collection3d(
                    Poly3DCollection(
                        mesh.vertices[mesh.faces],
                        facecolor=color,
                        edgecolor=(0.1, 0.1, 0.1, 0.25),
                        linewidths=0.2,
                    )
                )
            ax.set_xlim(lo[0], hi[0])
            ax.set_ylim(lo[1], hi[1])
            ax.set_zlim(lo[2], hi[2])
            ax.set_box_aspect(hi - lo)
            ax.view_init(elev=elev, azim=azim)
            ax.axis("off")
            out = out_dir / f"{prefix}_{name}.png"
            fig.savefig(str(out), dpi=100, bbox_inches="tight")
            paths.append(out)
        finally:
            plt.close(fig)
    return paths


def _draw_callouts(ax: Any, callouts: list[dict[str, Any]], bounds: Any) -> None:
    """Draw each dimension as a colored line between its two 3D endpoints with a
    text label offset outward from the part so it stays legible."""
    part_span = float(max(bounds[1] - bounds[0])) or 1.0
    offset = 0.07 * part_span
    for callout in callouts:
        p0, p1, text = callout["p0"], callout["p1"], callout["text"]
        ax.plot(
            [p0[0], p1[0]],
            [p0[1], p1[1]],
            [p0[2], p1[2]],
            color=CALLOUT_COLOR,
            linewidth=1.6,
            marker="|",
            markersize=6,
        )
        mid = ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2, (p0[2] + p1[2]) / 2)
        ax.text(
            mid[0],
            mid[1] - offset,
            mid[2] + offset,
            text,
            color=CALLOUT_COLOR,
            fontsize=8,
            ha="center",
            va="bottom",
        )


def mesh_is_watertight(stl_path: Path) -> bool:
    """Manifold check: a printable solid's exported mesh must be watertight.
    `force="mesh"` collapses any multi-body/Scene STL to one Trimesh so a
    degenerate export can't load as a `Scene` (which has no `.is_watertight`)
    and raise instead of returning False; empty geometry counts as NOT
    watertight. This keeps the runtime manifold gate (api/designs.build_design)
    fail-closed rather than throwing past its cleanup on a pathological mesh."""
    mesh = trimesh.load(str(stl_path), force="mesh")
    return bool(getattr(mesh, "is_watertight", False)) and len(mesh.faces) > 0


def write_preview_mesh(
    stl_path: Path, out_name: str = "part_preview.stl"
) -> Path | None:
    """Write a coarse, decimated preview mesh (default `part_preview.stl`) next to
    the real STL for the interactive 3D viewer. It's low-poly (good enough to orbit
    and judge the shape, NOT a clean manufacturing deliverable), so it can be served
    UNGATED — letting a customer see their part in 3D while the review gate keeps the
    full-res STEP/3MF/STL locked until the founder approves. Returns the preview
    path, or None if it couldn't be produced (the viewer then falls back)."""
    out = stl_path.with_name(out_name)
    try:
        mesh = trimesh.load(str(stl_path), force="mesh")
        if len(getattr(mesh, "faces", [])) == 0:
            return None
        if len(mesh.faces) > 800:
            try:
                target = max(300, len(mesh.faces) // 4)
                mesh = mesh.simplify_quadric_decimation(face_count=target)
            except Exception:
                pass  # decimation lib unavailable → ship the mesh as-is
        mesh.export(str(out))
        return out
    except Exception:
        return None


def mesh_body_count(stl_path: Path) -> int:
    """How many DISCONNECTED bodies the exported mesh has. A real printable part
    is ONE connected solid; >1 means floating/disjoint pieces (a common failure
    of generated geometry — e.g. an add-on that was placed but never fused to the
    body). Loaded with `force="mesh"` and split by connectivity."""
    mesh = trimesh.load(str(stl_path), force="mesh")
    if len(getattr(mesh, "faces", [])) == 0:
        return 0
    try:
        return int(mesh.body_count)
    except Exception:
        # Fall back to an explicit connected-component split.
        try:
            return len(mesh.split(only_watertight=False))
        except Exception:
            return 1


def heal_mesh_file(stl_path: Path) -> bool:
    """Manifold gate WITH automatic repair. Checks the exported mesh; if it isn't
    watertight, attempts a light, print-safe repair (merge coincident vertices,
    fix face winding + normals, fill small holes) and — if that makes it
    watertight — OVERWRITES the STL with the healed mesh so the part we ship is
    manifold. Returns the FINAL watertight status.

    This matters most for generated (freeform) geometry: CadQuery/OCC tessellation
    of a valid solid, or a slightly-imperfect boolean, can produce a mesh that is
    manifold in intent but has hairline gaps / inconsistent winding that make
    trimesh report it as not watertight. Healing recovers those without hiding a
    genuinely broken solid (a mesh with real holes/non-manifold edges stays
    non-watertight and is still rejected). A no-op for an already-watertight mesh
    (every template's default export), so it never changes those files."""
    mesh = trimesh.load(str(stl_path), force="mesh")
    if (
        bool(getattr(mesh, "is_watertight", False))
        and len(getattr(mesh, "faces", [])) > 0
    ):
        return True
    try:
        mesh.merge_vertices()
        trimesh.repair.fix_winding(mesh)
        trimesh.repair.fix_normals(mesh)
        try:
            trimesh.repair.fill_holes(mesh)  # needs networkx; best-effort
        except Exception:
            pass
    except Exception:
        return False
    healed = (
        bool(getattr(mesh, "is_watertight", False))
        and len(getattr(mesh, "faces", [])) > 0
    )
    if healed:
        original = stl_path.read_bytes()  # so a failed export can't corrupt the file
        try:
            mesh.export(str(stl_path))  # ship the repaired, watertight mesh
        except Exception:
            try:
                stl_path.write_bytes(original)  # restore the valid original
            except Exception:
                pass
            return False
    return healed
