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
