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
class DimCallout:
    """One labeled dimension arrow to draw on the preview (M5). `param` is the
    template param it annotates (so the renderer can look up its value + source
    marker); `p0`/`p1` are the two 3D endpoints in the part's own mm coordinate
    system; `label` is the human name shown ("span", "od A", "bore")."""

    param: str
    p0: tuple[float, float, float]
    p1: tuple[float, float, float]
    label: str


def _ephemeral_build_placeholder(_params: Any) -> cq.Workplane:
    """EphemeralTemplateSpecs build via the sandboxed subprocess (their generated
    code never runs in-process), so their build_fn is never called directly —
    api/designs.build_design dispatches them to api/sandbox instead. This
    placeholder makes any accidental in-process call loud rather than silent."""
    raise RuntimeError(
        "ephemeral (freeform) templates build in the sandbox, not via build_fn"
    )


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
    # (M5) Given a validated params instance, return the dimension callouts to
    # annotate on the preview render. Each template knows where its own key
    # dims attach on the geometry.
    callouts_fn: Callable[[Any], list[DimCallout]]


@dataclass(frozen=True)
class EphemeralTemplateSpec(TemplateSpec):
    """A one-off template authored by the LLM for a freeform request (Track B).
    Same shape as a TemplateSpec so the existing machinery treats it identically,
    plus `code`: the generated CadQuery source that api/sandbox runs to build it.
    `build_fn` is the placeholder above — never called; the sandbox is the build."""

    code: str = ""


_REGISTRY: dict[str, TemplateSpec] = {}
# Freeform templates live in a SEPARATE namespace so they never pollute the
# Track A catalog (GET /templates, the generic template test suite, all_templates)
# — but get_template still resolves them, so the intent flow / join / manifold
# gate treat them like any template.
_EPHEMERAL: dict[str, TemplateSpec] = {}
# Optional loader so a get_template miss can rehydrate an ephemeral template from
# disk after a restart. Set by api/freeform (keeps this leaf module import-free).
_EPHEMERAL_LOADER: Callable[[str], TemplateSpec | None] | None = None


def register_template(spec: TemplateSpec) -> TemplateSpec:
    if spec.template_id in _REGISTRY:
        raise ValueError(f"template_id '{spec.template_id}' is already registered")
    _REGISTRY[spec.template_id] = spec
    return spec


def register_ephemeral_template(spec: TemplateSpec) -> TemplateSpec:
    """Register (or replace) a freeform template. Replacement is allowed — the
    same id may be re-registered on rehydrate from disk."""
    _EPHEMERAL[spec.template_id] = spec
    return spec


def set_ephemeral_loader(loader: Callable[[str], TemplateSpec | None]) -> None:
    global _EPHEMERAL_LOADER
    _EPHEMERAL_LOADER = loader


def get_template(template_id: str) -> TemplateSpec | None:
    spec = _REGISTRY.get(template_id) or _EPHEMERAL.get(template_id)
    if spec is None and _EPHEMERAL_LOADER is not None:
        spec = _EPHEMERAL_LOADER(template_id)  # may register + return, or None
    return spec


def all_templates() -> dict[str, TemplateSpec]:
    """Track A catalog only — freeform templates are deliberately excluded."""
    return dict(_REGISTRY)


def all_ephemeral_templates() -> dict[str, TemplateSpec]:
    return dict(_EPHEMERAL)
