"""Knob-specific geometry sanity checks beyond the shared template suite
(tests/test_template_suite.py): bore depth should actually affect the part,
a D-shaft flat should actually be present, and the wall validators must
reject bad params."""

from __future__ import annotations

import trimesh
from cadquery import exporters
from pydantic import ValidationError
import pytest

from templates_lib.knob_appliance import KnobApplianceParams, build_knob

VALID_PARAMS = dict(
    shaft_type="round",
    shaft_dia_mm=6,
    shaft_depth_mm=12,
    knob_dia_mm=32,
    knob_height_mm=22,
    grip_style="smooth",
    pointer=False,
    spline_count=6,
)


def _volume_for(params: KnobApplianceParams, tmp_path, name: str) -> float:
    solid = build_knob(params)
    path = tmp_path / f"{name}.stl"
    exporters.export(solid, str(path))
    mesh = trimesh.load(str(path))
    assert mesh.is_watertight, f"{name}: mesh is not manifold"
    return mesh.volume


def test_deeper_bore_removes_more_material(tmp_path):
    shallow = KnobApplianceParams(**{**VALID_PARAMS, "shaft_depth_mm": 6})
    deep = KnobApplianceParams(**{**VALID_PARAMS, "shaft_depth_mm": 18})

    shallow_volume = _volume_for(shallow, tmp_path, "shallow")
    deep_volume = _volume_for(deep, tmp_path, "deep")

    assert deep_volume < shallow_volume


def test_d_shaft_flat_is_present(tmp_path):
    """A D-shaped bore is a round bore with a chord cut off — its cross-
    section area is *smaller* than a full circle, so it removes strictly
    less material than an equivalent plain round bore. If the flat weren't
    actually being cut, the D and round volumes would be identical."""
    round_params = KnobApplianceParams(**{**VALID_PARAMS, "shaft_type": "round"})
    d_params = KnobApplianceParams(**{**VALID_PARAMS, "shaft_type": "D"})

    round_volume = _volume_for(round_params, tmp_path, "round")
    d_volume = _volume_for(d_params, tmp_path, "d-shaft")

    assert d_volume > round_volume


@pytest.mark.parametrize("shaft_type", ["round", "D", "spline"])
def test_all_shaft_types_produce_manifold_mesh(tmp_path, shaft_type: str):
    params = KnobApplianceParams(**{**VALID_PARAMS, "shaft_type": shaft_type})
    _volume_for(params, tmp_path, shaft_type)


def test_radial_wall_below_min_wall_rejected():
    with pytest.raises(ValidationError, match="radial wall"):
        KnobApplianceParams(**{**VALID_PARAMS, "knob_dia_mm": 15, "shaft_dia_mm": 14})


def test_cap_wall_below_min_wall_rejected():
    with pytest.raises(ValidationError, match="top cap wall"):
        KnobApplianceParams(
            **{**VALID_PARAMS, "knob_height_mm": 15, "shaft_depth_mm": 14}
        )
