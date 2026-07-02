"""Tests for the bracket_shelf_l template: manifold mesh, min wall, param
validation, and export round-trips. Pure CadQuery/pydantic tests — no API."""

from __future__ import annotations

from pathlib import Path

import pytest
import trimesh
from cadquery import exporters
from pydantic import ValidationError

from templates_lib.bracket_shelf_l import (
    MIN_WALL_MM,
    BracketShelfLParams,
    build_bracket,
)

VALID_PARAMS = dict(
    span_mm=120,
    depth_mm=40,
    thickness_mm=4,
    screw_size="#8",
    screw_count=3,
    load_hint="medium",
)


def test_valid_params_construct():
    params = BracketShelfLParams(**VALID_PARAMS)
    assert params.span_mm == 120


@pytest.mark.parametrize("load_hint", ["light", "medium", "heavy"])
def test_mesh_is_manifold(tmp_path: Path, load_hint: str):
    params = BracketShelfLParams(**{**VALID_PARAMS, "load_hint": load_hint})
    solid = build_bracket(params)

    stl_path = tmp_path / "bracket.stl"
    exporters.export(solid, str(stl_path))

    mesh = trimesh.load(str(stl_path))
    assert mesh.is_watertight, f"mesh not manifold for load_hint={load_hint}"
    assert mesh.volume > 0


def test_min_wall_enforced_by_param_range():
    below_min = MIN_WALL_MM - 0.1
    with pytest.raises(ValidationError):
        BracketShelfLParams(**{**VALID_PARAMS, "thickness_mm": below_min})


@pytest.mark.parametrize(
    "overrides",
    [
        {"span_mm": 10},  # below range minimum
        {"span_mm": 500},  # above range maximum
        {"depth_mm": 5},  # below range minimum
        {"depth_mm": 500},  # above range maximum
        {"thickness_mm": 0.5},  # below MIN_WALL_MM
        {"thickness_mm": 50},  # above range maximum
        {"screw_count": 1},  # below range minimum
        {"screw_count": 20},  # above range maximum
        {"screw_size": "#12"},  # not a valid enum value
        {"load_hint": "extreme"},  # not a valid enum value
    ],
)
def test_out_of_range_params_rejected(overrides: dict):
    with pytest.raises(ValidationError):
        BracketShelfLParams(**{**VALID_PARAMS, **overrides})


def test_thickness_too_large_for_span_rejected():
    with pytest.raises(ValidationError, match="thickness_mm"):
        BracketShelfLParams(**{**VALID_PARAMS, "span_mm": 40, "thickness_mm": 11})


def test_screw_count_that_does_not_fit_rejected():
    with pytest.raises(ValidationError, match="screw_count"):
        BracketShelfLParams(
            **{**VALID_PARAMS, "span_mm": 45, "screw_size": "#10", "screw_count": 6}
        )


def test_depth_too_small_for_screw_size_rejected():
    with pytest.raises(ValidationError, match="depth_mm"):
        BracketShelfLParams(**{**VALID_PARAMS, "depth_mm": 5, "screw_size": "#10"})


def test_all_three_exports_produced_and_non_empty(tmp_path: Path):
    params = BracketShelfLParams(**VALID_PARAMS)
    solid = build_bracket(params)

    step_path = tmp_path / "bracket.step"
    threemf_path = tmp_path / "bracket.3mf"
    stl_path = tmp_path / "bracket.stl"

    exporters.export(solid, str(step_path))
    exporters.export(solid, str(threemf_path))
    exporters.export(solid, str(stl_path))

    for path in (step_path, threemf_path, stl_path):
        assert path.exists()
        assert path.stat().st_size > 0

    mesh = trimesh.load(str(stl_path))
    assert len(mesh.vertices) > 0
    assert len(mesh.faces) > 0
