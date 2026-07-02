"""Shared parameter-schema introspection: turns a template's pydantic params
model into a plain field list (name/type/min/max/choices/default/description).

Two callers need this same information for two different audiences: GET
/templates (api/templates.py) uses it to build the web UI's parameter form,
and the intent parser (api/intents.py) uses it to tell the vision LLM what
parameters/ranges each template actually accepts. A template's Field(...)
definitions in templates_lib/*.py stay the single source of truth for both.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def form_fields_for(model: type[BaseModel]) -> list[dict[str, Any]]:
    schema = model.model_json_schema()
    return [
        _field_from_property(name, prop) for name, prop in schema["properties"].items()
    ]


def _field_from_property(name: str, prop: dict[str, Any]) -> dict[str, Any]:
    choices = prop.get("enum")
    json_type = prop.get("type", "string")
    field_type = "choice" if choices else json_type
    if field_type not in ("choice", "number", "integer", "boolean"):
        field_type = "string"

    return {
        "name": name,
        "label": name.replace("_mm", " (mm)").replace("_", " ").strip().capitalize(),
        "type": field_type,
        "default": prop.get("default"),
        "minimum": prop.get("minimum"),
        "maximum": prop.get("maximum"),
        "choices": choices,
        "description": prop.get("description", ""),
    }
