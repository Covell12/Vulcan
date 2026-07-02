"""Export a CadQuery solid to STEP/3MF/STL and render a PNG preview.

Preview rendering uses matplotlib (Agg backend) over the exported STL's
triangle mesh rather than a CAD viewer, so it works headless with no display
or GPU — important since this runs inside the API process on a server.
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import trimesh  # noqa: E402
from cadquery import exporters  # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: E402

FACE_COLOR = (0.70, 0.75, 0.85, 1.0)
EDGE_COLOR = (0.2, 0.2, 0.2, 0.3)


def export_design(solid: cq.Workplane, design_dir: Path) -> dict[str, Path]:
    """Write STEP/3MF/STL + a PNG preview for `solid` into design_dir. Returns file paths."""
    design_dir.mkdir(parents=True, exist_ok=True)

    step_path = design_dir / "part.step"
    threemf_path = design_dir / "part.3mf"
    stl_path = design_dir / "part.stl"
    preview_path = design_dir / "preview.png"

    exporters.export(solid, str(step_path))
    exporters.export(solid, str(stl_path))
    exporters.export(solid, str(threemf_path))

    render_preview(stl_path, preview_path)

    return {
        "step": step_path,
        "threemf": threemf_path,
        "stl": stl_path,
        "preview_png": preview_path,
    }


def render_preview(stl_path: Path, preview_path: Path) -> None:
    mesh = trimesh.load(str(stl_path))
    fig = plt.figure(figsize=(6, 6))
    ax = fig.add_subplot(111, projection="3d")
    poly = Poly3DCollection(
        mesh.vertices[mesh.faces],
        facecolor=FACE_COLOR,
        edgecolor=EDGE_COLOR,
        linewidths=0.3,
    )
    ax.add_collection3d(poly)
    bounds = mesh.bounds
    ax.set_xlim(bounds[0][0], bounds[1][0])
    ax.set_ylim(bounds[0][1], bounds[1][1])
    ax.set_zlim(bounds[0][2], bounds[1][2])
    ax.set_box_aspect(bounds[1] - bounds[0])
    ax.view_init(elev=25, azim=-60)
    ax.axis("off")
    fig.savefig(str(preview_path), dpi=120, bbox_inches="tight")
    plt.close(fig)


def mesh_is_watertight(stl_path: Path) -> bool:
    """Manifold check: a printable solid's exported mesh must be watertight."""
    mesh = trimesh.load(str(stl_path))
    return bool(mesh.is_watertight)
