"""Tests for api/rendering.py — the headless preview pipeline.

Focus (M5 review #2): render_preview must ALWAYS close its matplotlib figure,
even when saving fails, so figures can't accumulate in matplotlib's global
registry inside the long-lived server process.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from cadquery import exporters
from PIL import Image

import trimesh

from api.rendering import (
    export_design,
    heal_mesh_file,
    mesh_is_watertight,
    render_preview,
    render_studio,
)
from templates_lib.registry import get_template


def test_heal_mesh_is_noop_for_watertight(tmp_path: Path):
    stl = _bracket_stl(tmp_path)
    before = stl.read_bytes()
    assert heal_mesh_file(stl) is True
    assert stl.read_bytes() == before  # unchanged — a valid mesh is not rewritten


def test_heal_mesh_repairs_a_holed_mesh(tmp_path: Path):
    # A box missing one face is not watertight; healing (merge + fill_holes)
    # should recover it AND rewrite the STL so the shipped mesh is manifold.
    box = trimesh.creation.box(extents=(10, 10, 10))
    holed = trimesh.Trimesh(
        vertices=box.vertices.copy(), faces=box.faces[:-1].copy(), process=False
    )
    stl = tmp_path / "holed.stl"
    holed.export(str(stl))
    assert mesh_is_watertight(stl) is False
    assert heal_mesh_file(stl) is True
    assert mesh_is_watertight(stl) is True  # the rewritten file is watertight


def test_heal_mesh_rejects_a_single_open_face(tmp_path: Path):
    # A lone triangle has an open boundary healing can't close into a solid.
    tri = trimesh.Trimesh(
        vertices=[[0, 0, 0], [10, 0, 0], [0, 10, 0]], faces=[[0, 1, 2]], process=False
    )
    stl = tmp_path / "tri.stl"
    tri.export(str(stl))
    assert heal_mesh_file(stl) is False


def test_body_count_single_vs_floating(tmp_path: Path):
    from api.rendering import mesh_body_count

    one = tmp_path / "one.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(str(one))
    assert mesh_body_count(one) == 1

    # Two disjoint boxes: watertight overall, but TWO bodies (floating pieces).
    a = trimesh.creation.box(extents=(5, 5, 5))
    b = trimesh.creation.box(extents=(5, 5, 5))
    b.apply_translation((40, 0, 0))
    two = tmp_path / "two.stl"
    trimesh.util.concatenate([a, b]).export(str(two))
    assert mesh_is_watertight(two) is True  # each body is closed…
    assert mesh_body_count(two) == 2  # …but it's not ONE connected solid


def test_write_preview_mesh(tmp_path: Path):
    from api.rendering import write_preview_mesh

    stl = _bracket_stl(tmp_path)
    preview = write_preview_mesh(stl)
    assert preview is not None and preview.name == "part_preview.stl"
    assert preview.exists() and preview.stat().st_size > 0
    # It must still be a loadable mesh (used by the 3D viewer).
    m = trimesh.load(str(preview), force="mesh")
    assert len(m.faces) > 0


def _bracket_stl(tmp_path: Path) -> Path:
    spec = get_template("bracket_shelf_l")
    solid = spec.build_fn(spec.params_model())
    stl = tmp_path / "part.stl"
    exporters.export(solid, str(stl))
    return stl


def test_render_preview_does_not_leak_figure_on_save_failure(tmp_path: Path):
    stl = _bracket_stl(tmp_path)
    before = len(plt.get_fignums())
    for _ in range(3):
        try:
            # A path under a non-existent directory makes savefig raise.
            render_preview(stl, tmp_path / "no_such_dir" / "out.png")
        except Exception:
            pass
    assert len(plt.get_fignums()) == before, "render_preview leaked a matplotlib figure"


def test_render_preview_closes_figure_on_success(tmp_path: Path):
    stl = _bracket_stl(tmp_path)
    before = len(plt.get_fignums())
    render_preview(stl, tmp_path / "out.png")
    assert (tmp_path / "out.png").exists()
    assert len(plt.get_fignums()) == before


# ---------------------------------------------------------------------------
# M10b studio product shot
# ---------------------------------------------------------------------------


def test_render_studio_single_part(tmp_path: Path):
    """The product shot renders a part over a gradient background with a
    silhouette edge — an image with real ember part pixels and no transparency."""
    stl = _bracket_stl(tmp_path)
    out = tmp_path / "studio.png"
    render_studio(
        [stl], out, [{"p0": (0, 0, 0), "p1": (180, 0, 0), "text": "span: 180 mm"}]
    )
    assert out.exists()
    arr = np.asarray(Image.open(out).convert("RGB"), dtype=int)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    ember = (r > 120) & (r - b > 45) & (g < r)
    assert ember.sum() > 1000, "expected a solid ember part in the studio shot"
    # Neutral gradient background: the top row is lighter than the bottom row.
    top = arr[2].mean()
    bottom = arr[-3].mean()
    assert top > bottom, "expected a top-lighter neutral gradient background"


def test_render_studio_assembly_uses_distinct_colors(tmp_path: Path):
    """Each part of an assembly gets its own palette colour (not both ember)."""
    a = trimesh.creation.box(extents=(30, 30, 30))
    b = trimesh.creation.box(extents=(30, 30, 30))
    b.apply_translation([50, 0, 0])
    pa, pb = tmp_path / "a.stl", tmp_path / "b.stl"
    a.export(str(pa))
    b.export(str(pb))
    out = tmp_path / "asm.png"
    render_studio([pa, pb], out)
    arr = np.asarray(Image.open(out).convert("RGB"), dtype=int)
    r, g, b_ = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    ember = (r > 150) & (r - b_ > 60) & (g < r)  # part 0 (ember)
    blue = (b_ > 140) & (b_ - r > 30)  # part 1 (blue in the palette)
    assert ember.sum() > 300 and blue.sum() > 300


def test_render_studio_empty_meshes_writes_background(tmp_path: Path):
    empty = tmp_path / "empty.stl"
    empty.write_text("solid empty\nendsolid empty\n")
    out = tmp_path / "bg.png"
    render_studio([empty], out)
    assert out.exists()


def test_mesh_is_watertight_true_for_real_solid(tmp_path: Path):
    assert mesh_is_watertight(_bracket_stl(tmp_path)) is True


def test_mesh_is_watertight_false_for_empty_stl(tmp_path: Path):
    """M5.5 review: an empty/degenerate STL must return False, not raise — the
    manifold gate stays fail-closed rather than throwing past its cleanup."""
    stl = tmp_path / "empty.stl"
    stl.write_text("solid empty\nendsolid empty\n")
    assert mesh_is_watertight(stl) is False


def test_export_design_with_callouts(tmp_path: Path):
    spec = get_template("bracket_shelf_l")
    params = spec.params_model(span_mm=180, depth_mm=50)
    solid = spec.build_fn(params)
    callouts = [
        {"p0": c.p0, "p1": c.p1, "text": f"{c.label}: {getattr(params, c.param):g} mm"}
        for c in spec.callouts_fn(params)
    ]
    before = len(plt.get_fignums())
    files = export_design(solid, tmp_path / "design", callouts)
    assert files["preview_png"].exists()
    assert files["preview_png"].stat().st_size > 0
    assert len(plt.get_fignums()) == before
