"""Adapter-specific geometry sanity checks beyond the shared template suite
(tests/test_template_suite.py): the bore must be a genuine through-hole, and
the cross-field validators (wall, id<od, aspect ratio, size ceiling) must
actually reject bad params."""

from __future__ import annotations

import pytest
import trimesh
from cadquery import exporters
from pydantic import ValidationError

from templates_lib.adapter_tube import (
    AdapterTubeParams,
    _revolve,
    _silhouette_points,
    build_adapter,
)

VALID_PARAMS = dict(
    od_a_mm=20,
    id_a_mm=14,
    od_b_mm=30,
    id_b_mm=22,
    engagement_a_mm=15,
    engagement_b_mm=20,
    taper=True,
)


def _mesh_for(params: AdapterTubeParams, tmp_path, name: str):
    solid = build_adapter(params)
    path = tmp_path / f"{name}.stl"
    exporters.export(solid, str(path))
    return trimesh.load(str(path))


@pytest.mark.parametrize("taper", [True, False])
def test_bore_is_open_through_hole(tmp_path, taper: bool):
    params = AdapterTubeParams(**{**VALID_PARAMS, "taper": taper})
    mesh = _mesh_for(params, tmp_path, f"taper-{taper}")
    assert mesh.is_watertight
    # A tube open at both ends is topologically a torus (genus 1): Euler
    # number 0. A solid with no hole through it would be 2 (sphere-like).
    assert mesh.euler_number == 0


def test_bore_removes_material_vs_solid_equivalent(tmp_path):
    params = AdapterTubeParams(**VALID_PARAMS)
    hollow_mesh = _mesh_for(params, tmp_path, "hollow")

    # Same OD envelope with no bore cut, using the template's own outer-
    # silhouette builder directly — the "solid-cylinder equivalent".
    outer_pts = _silhouette_points(
        params.od_a_mm / 2,
        params.od_b_mm / 2,
        params.engagement_a_mm,
        params.engagement_b_mm,
        params.taper,
        params.transition_length_mm,
    )
    solid_path = tmp_path / "solid.stl"
    exporters.export(_revolve(outer_pts), str(solid_path))
    solid_mesh = trimesh.load(str(solid_path))

    assert hollow_mesh.volume < solid_mesh.volume
    # The bore should account for a substantial fraction of removed material,
    # not a sliver — checks a real hole was cut, not a cosmetic nick.
    removed_fraction = (solid_mesh.volume - hollow_mesh.volume) / solid_mesh.volume
    assert removed_fraction > 0.1


def test_id_must_be_smaller_than_od():
    with pytest.raises(ValidationError, match="id_a_mm"):
        AdapterTubeParams(**{**VALID_PARAMS, "id_a_mm": 25, "od_a_mm": 20})


def test_wall_below_min_wall_rejected():
    with pytest.raises(ValidationError, match="wall thickness"):
        AdapterTubeParams(**{**VALID_PARAMS, "od_a_mm": 10, "id_a_mm": 9})


def test_implausible_engagement_ratio_rejected():
    with pytest.raises(ValidationError, match="engagement_a_mm"):
        AdapterTubeParams(**{**VALID_PARAMS, "od_a_mm": 120, "engagement_a_mm": 8})


def test_oversized_total_length_rejected():
    with pytest.raises(ValidationError, match="total length"):
        AdapterTubeParams(
            od_a_mm=120,
            id_a_mm=100,
            od_b_mm=20,
            id_b_mm=10,
            engagement_a_mm=100,
            engagement_b_mm=100,
            taper=True,
        )
