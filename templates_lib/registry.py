"""The template registry: a small, leaf module with no knowledge of any
specific template. Each template module registers itself on import (see
`templates_lib/__init__.py`, which imports every template module so this
happens automatically); nothing here ever imports a template module, to
avoid a circular import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Type

import cadquery as cq
from pydantic import BaseModel


@dataclass(frozen=True)
class TemplateSpec:
    """Everything the API and test suite need to treat a template generically."""

    template_id: str
    label: str
    params_model: Type[BaseModel]
    build_fn: Callable[[Any], cq.Workplane]
    # A params override (merged over params_model's defaults) that violates
    # MIN_WALL_MM for this template's geometry — used by the shared min-wall
    # test. Templates differ too much in field layout for one generic
    # violation to apply, so each template supplies its own known-bad values.
    min_wall_violation: dict[str, Any]
    # One of schemas/intent_spec.schema.json's `category` enum values — lets
    # the intent parser (M3) tell a template_id apart from the category the
    # vision LLM picks.
    category: str
    # Params (by name) that are fit-critical, per CLAUDE.md's dimension rules:
    # these may ONLY commit from source="user_measured" — the intent parser
    # (M3) enforces this as the critical-dim gate before status can become
    # "ready_for_design".
    critical_dims: tuple[str, ...]


_REGISTRY: dict[str, TemplateSpec] = {}


def register_template(spec: TemplateSpec) -> TemplateSpec:
    if spec.template_id in _REGISTRY:
        raise ValueError(f"template_id '{spec.template_id}' is already registered")
    _REGISTRY[spec.template_id] = spec
    return spec


def get_template(template_id: str) -> TemplateSpec | None:
    return _REGISTRY.get(template_id)


def all_templates() -> dict[str, TemplateSpec]:
    return dict(_REGISTRY)
