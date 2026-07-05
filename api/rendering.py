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
