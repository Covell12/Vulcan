"""Generic checks that run against every registered template, so a new
template (M3+) gets baseline coverage automatically just by registering
itself — see templates_lib/registry.py and templates_lib/__init__.py.

Per-template geometry sanity checks (things specific to one template's
shape, like the adapter's through-hole or the knob's D-flat) live in their
own test files, not here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import templates_lib  # noqa: F401  (import side effect: populates the registry)
from templates_lib.registry import all_templates
from tests.template_test_helpers import (
    assert_all_exports_non_empty,
    assert_mesh_is_manifold,
    assert_min_wall_violation_rejected,
)

TEMPLATE_IDS = sorted(all_templates())


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_default_params_produce_manifold_mesh(template_id: str, tmp_path: Path):
    assert_mesh_is_manifold(all_templates()[template_id], tmp_path)


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_min_wall_violation_is_rejected(template_id: str):
    assert_min_wall_violation_rejected(all_templates()[template_id])


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_exports_are_non_empty(template_id: str, tmp_path: Path):
    assert_all_exports_non_empty(all_templates()[template_id], tmp_path)


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_callouts_declared_and_valid(template_id: str):
    """Every template must declare preview callouts (M5), each referencing a
    real param and giving two distinct 3D endpoints."""
    spec = all_templates()[template_id]
    params = spec.params_model()
    field_names = set(spec.params_model.model_fields)

    callouts = spec.callouts_fn(params)
    assert callouts, f"{template_id} declares no callouts"
    for c in callouts:
        assert (
            c.param in field_names
        ), f"{template_id}: callout param '{c.param}' is not a field"
        assert len(c.p0) == 3 and len(c.p1) == 3
        assert c.p0 != c.p1, f"{template_id}: callout '{c.label}' has zero length"
        assert c.label
