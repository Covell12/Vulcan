"""Tests for api/rendering.py — the headless preview pipeline.

Focus (M5 review #2): render_preview must ALWAYS close its matplotlib figure,
even when saving fails, so figures can't accumulate in matplotlib's global
registry inside the long-lived server process.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from cadquery import exporters

import trimesh

from api.rendering import (
    export_design,
    heal_mesh_file,
    mesh_is_watertight,
    render_preview,
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
