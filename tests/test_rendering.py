"""Tests for api/rendering.py — the headless preview pipeline.

Focus (M5 review #2): render_preview must ALWAYS close its matplotlib figure,
even when saving fails, so figures can't accumulate in matplotlib's global
registry inside the long-lived server process.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from cadquery import exporters

from api.rendering import export_design, render_preview
from templates_lib.registry import get_template


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
