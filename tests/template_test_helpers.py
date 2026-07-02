"""Shared, template-agnostic checks. Not a test module itself (pytest only
collects test_*.py/*_test.py) — see tests/test_template_suite.py for how
these get wired up as parametrized tests against every registered template.
"""

from __future__ import annotations

from pathlib import Path

import trimesh
from cadquery import exporters
from pydantic import ValidationError

from templates_lib.registry import TemplateSpec


def build_default_solid(spec: TemplateSpec):
    """Every template's params model has full field defaults, so calling it
    with no arguments always yields one valid, self-consistent example."""
    params = spec.params_model()
    return spec.build_fn(params)


def assert_mesh_is_manifold(spec: TemplateSpec, tmp_path: Path) -> None:
    solid = build_default_solid(spec)
    stl_path = tmp_path / f"{spec.template_id}.stl"
    exporters.export(solid, str(stl_path))

    mesh = trimesh.load(str(stl_path))
    assert (
        mesh.is_watertight
    ), f"{spec.template_id}: default-params mesh is not manifold"
    assert mesh.volume > 0, f"{spec.template_id}: default-params mesh has zero volume"


def assert_min_wall_violation_rejected(spec: TemplateSpec) -> None:
    """Templates differ too much in field layout for one generic violation to
    apply everywhere, so each template supplies its own known-bad override
    (TemplateSpec.min_wall_violation) merged over its own defaults."""
    if not spec.min_wall_violation:
        return
    try:
        spec.params_model(**spec.min_wall_violation)
    except ValidationError:
        return
    raise AssertionError(
        f"{spec.template_id}: min_wall_violation params {spec.min_wall_violation} "
        "were accepted instead of rejected"
    )


def assert_all_exports_non_empty(spec: TemplateSpec, tmp_path: Path) -> None:
    solid = build_default_solid(spec)
    step_path = tmp_path / f"{spec.template_id}.step"
    threemf_path = tmp_path / f"{spec.template_id}.3mf"
    stl_path = tmp_path / f"{spec.template_id}.stl"

    exporters.export(solid, str(step_path))
    exporters.export(solid, str(threemf_path))
    exporters.export(solid, str(stl_path))

    for path in (step_path, threemf_path, stl_path):
        assert path.exists(), f"{spec.template_id}: {path.name} was not created"
        assert path.stat().st_size > 0, f"{spec.template_id}: {path.name} is empty"
