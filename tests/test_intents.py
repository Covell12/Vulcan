"""Tests for POST /intents and POST /intents/{id}/answers. Mocked at the
parse_intent interface (api.intents.parse_intent) — no network in pytest,
per the milestone's testing requirement.

Covers: intent -> answers -> ready_for_design round-trip; the schema-
validation retry path; answer updates setting dimension sources correctly;
and the critical-dim gate, which CLAUDE.md marks non-negotiable (a
fit-critical dimension may only ever commit from source="user_measured",
and status may only become "ready_for_design" once every one of them has).
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.intents import INTENTS_DIR
from api.main import app

client = TestClient(app)

BRACKET_INTENT = {
    "intent_id": "ignored",
    "status": "needs_answers",
    "category": "bracket",
    "template_id": "bracket_shelf_l",
    "description": "A shelf bracket to hold a wooden shelf under a desk.",
    "context_notes": "Indoor, light load.",
    "material_suggestion": "PETG",
    "out_of_scope_reason": None,
    "dimensions": [
        {
            "name": "span_mm",
            "value_mm": 150.0,
            "source": "assumed",
            "confidence": 0.4,
            "critical": True,
            "cross_check": None,
        },
        {
            "name": "depth_mm",
            "value_mm": 40.0,
            "source": "assumed",
            "confidence": 0.4,
            "critical": True,
            "cross_check": None,
        },
    ],
    "questions": [
        {
            "question_id": "q_span",
            "dim_name": "span_mm",
            "prompt": "How far should the shelf stick out, in mm?",
            "kind": "measure_mm",
            "choices": None,
            "overlay": {
                "photo_index": 0,
                "shape": "arrow",
                "points": [[0.3, 0.4], [0.6, 0.4]],
            },
        },
        {
            "question_id": "q_depth",
            "dim_name": "depth_mm",
            "prompt": "How wide should the bracket be, in mm?",
            "kind": "measure_mm",
            "choices": None,
            "overlay": {"photo_index": 0, "shape": "circle", "points": [[0.5, 0.5]]},
        },
    ],
}


def _one_photo_files() -> list[tuple]:
    return [("photos", ("p.jpg", io.BytesIO(b"fake-jpeg-bytes"), "image/jpeg"))]


@pytest.fixture
def cleanup_intents() -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    for intent_id in created:
        (INTENTS_DIR / f"{intent_id}.json").unlink(missing_ok=True)
    if INTENTS_DIR.exists() and not any(INTENTS_DIR.iterdir()):
        shutil.rmtree(INTENTS_DIR.parent, ignore_errors=True)


def _create_intent(
    cleanup_intents: list[str],
    intent: dict = BRACKET_INTENT,
    text: str = "bracket please",
) -> dict:
    with patch("api.intents.parse_intent", return_value=intent):
        response = client.post(
            "/intents", files=_one_photo_files(), data={"text": text}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    cleanup_intents.append(body["intent_id"])
    return body


# ---------------------------------------------------------------------------
# POST /intents
# ---------------------------------------------------------------------------


def test_create_intent_round_trip(cleanup_intents: list[str]):
    body = _create_intent(cleanup_intents)

    assert body["status"] == "needs_answers"
    assert body["template_id"] == "bracket_shelf_l"
    assert (INTENTS_DIR / f"{body['intent_id']}.json").exists()

    fetched = client.get(f"/intents/{body['intent_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == body


def test_create_intent_passes_photos_and_annotation_to_provider(
    cleanup_intents: list[str],
):
    annotation = [{"photo_index": 0, "points": [[0.2, 0.3], [0.25, 0.32]]}]
    with patch("api.intents.parse_intent", return_value=BRACKET_INTENT) as mock_parse:
        response = client.post(
            "/intents",
            files=_one_photo_files(),
            data={"text": "bracket please", "annotation": json.dumps(annotation)},
        )
    assert response.status_code == 200
    cleanup_intents.append(response.json()["intent_id"])

    call = mock_parse.call_args
    photos, passed_annotation, text, catalog = call.args
    assert len(photos) == 1
    assert passed_annotation == annotation
    assert text == "bracket please"
    assert any(t["template_id"] == "bracket_shelf_l" for t in catalog)


def test_zero_photos_rejected():
    response = client.post("/intents", files=[], data={"text": "bracket please"})
    assert response.status_code == 422


def test_too_many_photos_rejected():
    files = [
        ("photos", (f"p{i}.jpg", io.BytesIO(b"x"), "image/jpeg")) for i in range(4)
    ]
    response = client.post("/intents", files=files, data={"text": "bracket please"})
    assert response.status_code == 422


def test_invalid_annotation_json_rejected():
    response = client.post(
        "/intents",
        files=_one_photo_files(),
        data={"text": "bracket please", "annotation": "{not json"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Schema-validation retry path
# ---------------------------------------------------------------------------


def test_invalid_response_retried_once_then_succeeds(cleanup_intents: list[str]):
    invalid = {
        "intent_id": "x",
        "status": "bogus_not_an_enum_value",
    }  # missing required fields too
    with patch(
        "api.intents.parse_intent", side_effect=[invalid, BRACKET_INTENT]
    ) as mock_parse:
        response = client.post(
            "/intents", files=_one_photo_files(), data={"text": "bracket please"}
        )

    assert response.status_code == 200, response.text
    cleanup_intents.append(response.json()["intent_id"])
    assert mock_parse.call_count == 2
    assert mock_parse.call_args_list[1].kwargs.get("retry_feedback")


def test_invalid_response_twice_returns_502():
    invalid = {"intent_id": "x", "status": "bogus_not_an_enum_value"}
    with patch(
        "api.intents.parse_intent", side_effect=[invalid, invalid]
    ) as mock_parse:
        response = client.post(
            "/intents", files=_one_photo_files(), data={"text": "bracket please"}
        )

    assert response.status_code == 502
    assert mock_parse.call_count == 2


# ---------------------------------------------------------------------------
# The critical-dim gate (non-negotiable per CLAUDE.md)
# ---------------------------------------------------------------------------


def test_unanswered_critical_dims_keep_needs_answers(cleanup_intents: list[str]):
    body = _create_intent(cleanup_intents)
    assert body["status"] == "needs_answers"

    # Answer only ONE of the two critical dims.
    response = client.post(
        f"/intents/{body['intent_id']}/answers",
        json={"answers": [{"question_id": "q_span", "measure_mm": 160.0}]},
    )
    assert response.status_code == 200
    updated = response.json()

    assert (
        updated["status"] == "needs_answers"
    ), "must NOT be ready_for_design with an unanswered critical dim"
    dims = {d["name"]: d for d in updated["dimensions"]}
    assert dims["span_mm"]["source"] == "user_measured"
    assert dims["span_mm"]["value_mm"] == 160.0
    assert dims["span_mm"]["confidence"] == 1.0
    assert dims["depth_mm"]["source"] == "assumed"  # untouched


def test_all_critical_dims_answered_reaches_ready_for_design(
    cleanup_intents: list[str],
):
    body = _create_intent(cleanup_intents)

    response = client.post(
        f"/intents/{body['intent_id']}/answers",
        json={
            "answers": [
                {"question_id": "q_span", "measure_mm": 160.0},
                {"question_id": "q_depth", "measure_mm": 45.0},
            ]
        },
    )
    assert response.status_code == 200
    updated = response.json()

    assert updated["status"] == "ready_for_design"
    for dim in updated["dimensions"]:
        if dim["critical"]:
            assert dim["source"] == "user_measured"
            assert dim["confidence"] == 1.0


def test_critical_dim_missing_from_provider_output_is_synthesized(
    cleanup_intents: list[str],
):
    """Even if the provider forgets a critical dim entirely, the gate must
    still require it before allowing ready_for_design — never trust the
    provider's own opinion of what's critical or complete."""
    incomplete = {
        **BRACKET_INTENT,
        "dimensions": [BRACKET_INTENT["dimensions"][0]],
        "questions": [BRACKET_INTENT["questions"][0]],
    }
    body = _create_intent(cleanup_intents, intent=incomplete)

    dim_names = {d["name"] for d in body["dimensions"]}
    assert (
        "depth_mm" in dim_names
    ), "the gate must synthesize the missing critical dimension"
    depth_dim = next(d for d in body["dimensions"] if d["name"] == "depth_mm")
    assert depth_dim["critical"] is True
    assert depth_dim["source"] == "assumed"

    question_dims = {q["dim_name"] for q in body["questions"]}
    assert (
        "depth_mm" in question_dims
    ), "the gate must synthesize a question for the missing critical dimension"
    assert body["status"] == "needs_answers"


def test_provider_marking_noncritical_dim_critical_is_overridden(
    cleanup_intents: list[str],
):
    """The provider's `critical` flag is never trusted — only
    templates_lib.registry's critical_dims decides."""
    tampered = json.loads(json.dumps(BRACKET_INTENT))
    tampered["dimensions"].append(
        {
            "name": "thickness_mm",
            "value_mm": 4.0,
            "source": "assumed",
            "confidence": 0.5,
            "critical": True,  # thickness_mm is NOT in bracket_shelf_l's critical_dims
            "cross_check": None,
        }
    )
    body = _create_intent(cleanup_intents, intent=tampered)
    thickness_dim = next(d for d in body["dimensions"] if d["name"] == "thickness_mm")
    assert thickness_dim["critical"] is False


def test_out_of_scope_status_is_respected(cleanup_intents: list[str]):
    out_of_scope = {
        **BRACKET_INTENT,
        "template_id": None,
        "category": "other",
        "out_of_scope_reason": "Requested part exceeds the 250mm bounding box limit.",
        "dimensions": [],
        "questions": [],
    }
    body = _create_intent(cleanup_intents, intent=out_of_scope)
    assert body["status"] == "out_of_scope"


# ---------------------------------------------------------------------------
# POST /intents/{id}/answers — other answer kinds and error handling
# ---------------------------------------------------------------------------


def test_confirm_answer_marks_assumed_value_as_measured(cleanup_intents: list[str]):
    intent_with_confirm = json.loads(json.dumps(BRACKET_INTENT))
    intent_with_confirm["questions"].append(
        {
            "question_id": "q_confirm_depth",
            "dim_name": "depth_mm",
            "prompt": "Is 40mm correct?",
            "kind": "confirm",
            "choices": None,
            "overlay": None,
        }
    )
    body = _create_intent(cleanup_intents, intent=intent_with_confirm)

    response = client.post(
        f"/intents/{body['intent_id']}/answers",
        json={
            "answers": [
                {"question_id": "q_span", "measure_mm": 160.0},
                {"question_id": "q_confirm_depth", "confirm": True},
            ]
        },
    )
    assert response.status_code == 200
    updated = response.json()
    dims = {d["name"]: d for d in updated["dimensions"]}
    assert dims["depth_mm"]["source"] == "user_measured"
    assert dims["depth_mm"]["value_mm"] == 40.0  # unchanged, just confirmed
    assert updated["status"] == "ready_for_design"


def test_choice_answer_updates_material_suggestion(cleanup_intents: list[str]):
    intent_with_choice = json.loads(json.dumps(BRACKET_INTENT))
    intent_with_choice["questions"].append(
        {
            "question_id": "q_material",
            "dim_name": None,
            "prompt": "Which material?",
            "kind": "choice",
            "choices": ["PLA", "PETG", "TPU", "CF-PETG"],
            "overlay": None,
        }
    )
    body = _create_intent(cleanup_intents, intent=intent_with_choice)

    response = client.post(
        f"/intents/{body['intent_id']}/answers",
        json={"answers": [{"question_id": "q_material", "choice": "TPU"}]},
    )
    assert response.status_code == 200
    assert response.json()["material_suggestion"] == "TPU"


def test_answers_unknown_question_id_rejected(cleanup_intents: list[str]):
    body = _create_intent(cleanup_intents)
    response = client.post(
        f"/intents/{body['intent_id']}/answers",
        json={"answers": [{"question_id": "nope"}]},
    )
    assert response.status_code == 422


def test_answers_unknown_intent_id_returns_404():
    response = client.post("/intents/does-not-exist/answers", json={"answers": []})
    assert response.status_code == 404


def test_get_unknown_intent_returns_404():
    response = client.get("/intents/does-not-exist")
    assert response.status_code == 404
