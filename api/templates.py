"""GET /templates: describes every registered template's parameter form.

The web UI has no hardcoded knowledge of any template's fields — it fetches
this endpoint and builds the parameter form from the response. The field
list comes from api/param_schema.py, which both this endpoint and the intent
parser (api/intents.py) share.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

import templates_lib  # noqa: F401  (import side effect: populates the registry)
from api.param_schema import form_fields_for
from templates_lib.registry import all_templates

router = APIRouter()


class FormField(BaseModel):
    name: str
    label: str
    type: str  # "number" | "integer" | "boolean" | "choice"
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] | None = None
    description: str = ""


class TemplateDescription(BaseModel):
    template_id: str
    label: str
    fields: list[FormField]


@router.get("/templates", response_model=list[TemplateDescription])
def list_templates() -> list[TemplateDescription]:
    return [
        TemplateDescription(
            template_id=spec.template_id,
            label=spec.label,
            fields=[FormField(**f) for f in form_fields_for(spec.params_model)],
        )
        for spec in sorted(all_templates().values(), key=lambda s: s.template_id)
    ]
