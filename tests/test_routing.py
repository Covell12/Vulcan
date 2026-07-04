"""Part A routing regression: the intent router must offer/recommend freeform
when the matched template can't really make the part (low template_fit or any
unsupported_features), and must stay on the template for clean fits. The vision
provider is mocked — these assert the ROUTING LOGIC given a provider's output;
the real provider's routing on the same 8 requests is checked in a live eval
(see the milestone write-up), not here.
"""

from __future__ import annotations

import io
from typing import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api.intents import INTENTS_DIR
from api.main import app

client = TestClient(app)


def _spec(**over):
    base = {
        "intent_id": "x",
        "status": "needs_answers",
        "category": "other",
        "template_id": None,
        "description": "d",
        "material_suggestion": "PETG",
        "out_of_scope_reason": None,
        "template_fit": None,
        "unsupported_features": [],
        "dimensions": [],
        "questions": [],
    }
    base.update(over)
    return base


# 4 that MUST route to freeform (recommended) + 4 that must stay on a template.
# The mocked provider output reflects an HONEST response for each request.
ROUTING_CASES = [
    # --- MUST recommend freeform ---
    (
        "lego bridge plate",
        "a flat plate that bridges two Lego baseplates with a 8mm stud grid on top",
        _spec(category="other", template_id=None, unsupported_features=["stud grid"]),
        True,
    ),
    (
        "curved cable guide",
        "a curved cable guide that routes a cable around a corner",
        _spec(
            category="other", template_id=None, unsupported_features=["curved profile"]
        ),
        True,
    ),
    (
        "hole grid tray",
        "a shelf bracket but the shelf face needs a 8mm grid of 4mm holes",
        # matched a bracket, but the grid is unsupported -> recommend freeform
        _spec(
            category="bracket",
            template_id="bracket_shelf_l",
            template_fit=0.55,
            unsupported_features=["hole grid"],
        ),
        True,
    ),
    (
        "two-piece clamp",
        "a two-piece clamp that bolts around a 30mm pipe",
        _spec(
            category="clip",
            template_id=None,
            unsupported_features=["two mating pieces", "bolt bosses"],
        ),
        True,
    ),
    # --- MUST stay on a template ---
    (
        "shelf bracket",
        "an L bracket to hold a shelf under my desk",
        _spec(category="bracket", template_id="bracket_shelf_l", template_fit=0.92),
        False,
    ),
    (
        "tube adapter",
        "an adapter between a 30mm and a 40mm hose",
        _spec(category="adapter", template_id="adapter_tube", template_fit=0.88),
        False,
    ),
    (
        "appliance knob",
        "a replacement knob for my stove dial",
        _spec(category="knob", template_id="knob_appliance", template_fit=0.9),
        False,
    ),
    (
        "corner brace",
        "a simple right-angle brace to stiffen a corner joint",
        _spec(category="bracket", template_id="bracket_shelf_l", template_fit=0.8),
        False,
    ),
]


@pytest.fixture
def cleanup_intents() -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    import shutil

    for iid in created:
        (INTENTS_DIR / f"{iid}.json").unlink(missing_ok=True)
        shutil.rmtree(INTENTS_DIR / iid, ignore_errors=True)


def _photo() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (48, 36), (200, 200, 200)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.mark.parametrize(
    "label,text,spec,expect_freeform", ROUTING_CASES, ids=[c[0] for c in ROUTING_CASES]
)
def test_routing(cleanup_intents, label, text, spec, expect_freeform):
    with patch("api.intents.parse_intent", return_value=dict(spec)):
        r = client.post(
            "/intents",
            files=[("photos", ("p.jpg", io.BytesIO(_photo()), "image/jpeg"))],
            data={"text": text},
        )
    body = r.json()
    cleanup_intents.append(body["intent_id"])

    # Freeform is ALWAYS available for an in-scope request (the override).
    assert body["freeform_available"] is True
    # ...and RECOMMENDED exactly when the template is a poor/absent fit.
    assert body["freeform_recommended"] is expect_freeform, (
        f"{label}: expected freeform_recommended={expect_freeform}, "
        f"got {body['freeform_recommended']} (template_id={body.get('template_id')}, "
        f"fit={spec['template_fit']}, unsupported={spec['unsupported_features']})"
    )


def test_out_of_scope_offers_no_freeform(cleanup_intents):
    spec = _spec(status="out_of_scope", out_of_scope_reason="too big")
    with patch("api.intents.parse_intent", return_value=dict(spec)):
        r = client.post(
            "/intents",
            files=[("photos", ("p.jpg", io.BytesIO(_photo()), "image/jpeg"))],
            data={"text": "a 2 meter beam"},
        )
    body = r.json()
    cleanup_intents.append(body["intent_id"])
    assert body["freeform_available"] is False
    assert body["freeform_recommended"] is False
