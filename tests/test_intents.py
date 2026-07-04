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
from PIL import Image

from api.depth_provider import DepthProviderError, ScaleEstimate
from api.designs import EXPORTS_DIR
from api.intents import DATA_DIR, INTENTS_DIR
from api.main import app
from api.vision_provider import VisionProviderError

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
        # M5.5 persists photos under data/intents/<id>/photos/ — remove the dir.
        shutil.rmtree(INTENTS_DIR / intent_id, ignore_errors=True)
    if INTENTS_DIR.exists() and not any(INTENTS_DIR.iterdir()):
        shutil.rmtree(INTENTS_DIR.parent, ignore_errors=True)


def _valid_photo_files() -> list[tuple]:
    """A REAL (decodable) JPEG, so the ghost composite can actually load it.
    The default _one_photo_files() sends junk bytes, which is fine for paths
    that never open the image but not for the composite tests."""
    buf = io.BytesIO()
    Image.new("RGB", (240, 180), (200, 205, 210)).save(buf, "JPEG")
    return [("photos", ("p.jpg", io.BytesIO(buf.getvalue()), "image/jpeg"))]


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


def _create_intent_with_depth(
    cleanup_intents: list[str],
    depth: dict[str, float],
    intent: dict = BRACKET_INTENT,
) -> dict:
    """Create an intent with a mocked depth prior: `depth` maps dim_name ->
    metric mm. Those dims come back source="depth_inferred"."""

    def fake_estimate(photo, regions):
        return [
            ScaleEstimate(r.dim_name, depth[r.dim_name], 0.45)
            for r in regions
            if r.dim_name in depth
        ]

    with patch(
        "api.intents.parse_intent", return_value=json.loads(json.dumps(intent))
    ), patch("api.intents.estimate_scale", side_effect=fake_estimate):
        response = client.post(
            "/intents", files=_one_photo_files(), data={"text": "bracket please"}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    cleanup_intents.append(body["intent_id"])
    return body


def _dim(intent: dict, name: str) -> dict:
    return next(d for d in intent["dimensions"] if d["name"] == name)


# A vision output for the M5 join tests: assumed dims + choice questions that
# carry a provider suggested_value (screw_size), one to be answered (load_hint).
JOIN_VISION = {
    "intent_id": "x",
    "status": "needs_answers",
    "category": "bracket",
    "template_id": "bracket_shelf_l",
    "description": "A shelf bracket.",
    "context_notes": "",
    "material_suggestion": "PETG",
    "out_of_scope_reason": None,
    "dimensions": [
        {
            "name": "span_mm",
            "value_mm": 120.0,
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
        {
            "name": "thickness_mm",
            "value_mm": 5.0,
            "source": "assumed",
            "confidence": 0.4,
            "critical": False,
            "cross_check": None,
        },
    ],
    "questions": [
        {
            "question_id": "q_span",
            "dim_name": "span_mm",
            "prompt": "span?",
            "kind": "measure_mm",
            "choices": None,
            "overlay": None,
            "suggested_value": None,
            "chosen_value": None,
        },
        {
            "question_id": "q_depth",
            "dim_name": "depth_mm",
            "prompt": "depth?",
            "kind": "measure_mm",
            "choices": None,
            "overlay": None,
            "suggested_value": None,
            "chosen_value": None,
        },
        {
            "question_id": "q_load",
            "dim_name": "load_hint",
            "prompt": "load?",
            "kind": "choice",
            "choices": ["light", "medium", "heavy"],
            "overlay": None,
            "suggested_value": "medium",
            "chosen_value": None,
        },
        {
            "question_id": "q_screw",
            "dim_name": "screw_size",
            "prompt": "screw?",
            "kind": "choice",
            "choices": ["#6", "#8", "#10"],
            "overlay": None,
            "suggested_value": "#10",
            "chosen_value": None,
        },
    ],
}


def _confirm_all_critical(intent_id: str) -> None:
    client.post(
        f"/intents/{intent_id}/answers",
        json={
            "answers": [
                {"question_id": "q_span", "measure_mm": 200.0},
                {"question_id": "q_depth", "measure_mm": 50.0},
            ]
        },
    )


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


# ---------------------------------------------------------------------------
# Provider errors -> clean 502 (never a bare 500) — the fix (a) contract.
# ---------------------------------------------------------------------------


def test_vision_provider_error_returns_502():
    with patch(
        "api.intents.parse_intent",
        side_effect=VisionProviderError("OpenAI request failed: rate limited"),
    ):
        response = client.post(
            "/intents", files=_one_photo_files(), data={"text": "bracket please"}
        )
    assert response.status_code == 502
    assert "rate limited" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Depth prior (M4): assumed dims get depth_inferred proposals; failures degrade.
# ---------------------------------------------------------------------------


def test_depth_prior_fills_assumed_dims(cleanup_intents: list[str]):
    body = _create_intent_with_depth(cleanup_intents, depth={"span_mm": 205.0})
    span = _dim(body, "span_mm")
    assert span["source"] == "depth_inferred"
    assert span["value_mm"] == 205.0
    assert 0 < span["confidence"] <= 0.5
    # A critical dim with only a depth prior is still NOT satisfied.
    assert body["status"] == "needs_answers"
    # A dim with no depth estimate stays "assumed".
    assert _dim(body, "depth_mm")["source"] == "assumed"


def test_depth_provider_failure_degrades_gracefully(cleanup_intents: list[str]):
    with patch(
        "api.intents.parse_intent", return_value=json.loads(json.dumps(BRACKET_INTENT))
    ), patch(
        "api.intents.estimate_scale", side_effect=DepthProviderError("replicate down")
    ):
        response = client.post(
            "/intents", files=_one_photo_files(), data={"text": "bracket please"}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    cleanup_intents.append(body["intent_id"])
    # Depth failure must not break intent creation — dims just stay "assumed".
    assert _dim(body, "span_mm")["source"] == "assumed"


# ---------------------------------------------------------------------------
# Cross-check (M4): >20% disagreement with the depth prior is re-asked, never
# silently committed or overridden (CLAUDE.md rule 3).
# ---------------------------------------------------------------------------


def _answer(intent_id: str, question_id: str, value: float) -> dict:
    r = client.post(
        f"/intents/{intent_id}/answers",
        json={"answers": [{"question_id": question_id, "measure_mm": value}]},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_crosscheck_mm_cm_mistake_is_flagged(cleanup_intents: list[str]):
    # Depth says ~250mm; user types 25 (they measured in cm). 10x off.
    body = _create_intent_with_depth(cleanup_intents, depth={"span_mm": 250.0})
    updated = _answer(body["intent_id"], "q_span", 25.0)

    span = _dim(updated, "span_mm")
    assert span["source"] != "user_measured", "a unit mistake must NOT commit"
    cc = span["cross_check"]
    assert cc["status"] == "mismatch_reask"
    assert cc["depth_value_mm"] == 250.0
    assert cc["ratio"] == pytest.approx(0.1, rel=1e-3)
    assert updated["status"] == "needs_answers"

    # A re-ask question naming BOTH values + the unit hint is present.
    reask = [q for q in updated["questions"] if q.get("dim_name") == "span_mm"]
    assert any("25" in q["prompt"] and "250" in q["prompt"] for q in reask)
    assert any("centimet" in q["prompt"].lower() for q in reask)


def test_crosscheck_inch_mistake_is_flagged(cleanup_intents: list[str]):
    # Depth says ~254mm; user types 10 (they measured in inches). 25.4x off.
    body = _create_intent_with_depth(cleanup_intents, depth={"span_mm": 254.0})
    updated = _answer(body["intent_id"], "q_span", 10.0)

    span = _dim(updated, "span_mm")
    assert span["source"] != "user_measured"
    assert span["cross_check"]["status"] == "mismatch_reask"
    reask = [q for q in updated["questions"] if q.get("dim_name") == "span_mm"]
    assert any("inch" in q["prompt"].lower() for q in reask)


def test_crosscheck_reconfirm_same_value_commits_override(cleanup_intents: list[str]):
    body = _create_intent_with_depth(cleanup_intents, depth={"span_mm": 250.0})
    iid = body["intent_id"]

    # First submission is flagged...
    flagged = _answer(iid, "q_span", 25.0)
    assert _dim(flagged, "span_mm")["source"] != "user_measured"

    # ...re-submitting the SAME value is an explicit override => commit.
    committed = _answer(iid, "q_span", 25.0)
    span = _dim(committed, "span_mm")
    assert span["source"] == "user_measured"
    assert span["value_mm"] == 25.0
    assert span["confidence"] == 1.0
    assert span["cross_check"]["status"] == "ok"
    # the re-ask question is pruned once resolved
    assert not any(q["question_id"] == "reask-span_mm" for q in committed["questions"])


def test_crosscheck_corrected_value_within_tolerance_commits(
    cleanup_intents: list[str],
):
    body = _create_intent_with_depth(cleanup_intents, depth={"span_mm": 250.0})
    iid = body["intent_id"]

    _answer(iid, "q_span", 25.0)  # flagged
    # Now the user enters a corrected value close to the prior => commits.
    corrected = _answer(iid, "q_span", 248.0)
    span = _dim(corrected, "span_mm")
    assert span["source"] == "user_measured"
    assert span["value_mm"] == 248.0
    assert span["cross_check"]["status"] == "ok"


def test_crosscheck_depth_unavailable_commits_normally(cleanup_intents: list[str]):
    # No depth prior (default DEPTH_PROVIDER=none path): everything commits, and
    # the cross_check records status "unavailable".
    body = _create_intent(cleanup_intents)
    updated = _answer(body["intent_id"], "q_span", 25.0)
    span = _dim(updated, "span_mm")
    assert span["source"] == "user_measured"
    assert span["value_mm"] == 25.0
    assert span["cross_check"]["status"] == "unavailable"


def test_crosscheck_within_tolerance_first_try_commits(cleanup_intents: list[str]):
    body = _create_intent_with_depth(cleanup_intents, depth={"span_mm": 250.0})
    # 240 is within 20% of 250 -> commits immediately, no flag.
    updated = _answer(body["intent_id"], "q_span", 240.0)
    span = _dim(updated, "span_mm")
    assert span["source"] == "user_measured"
    assert span["cross_check"]["status"] == "ok"


def test_confirm_does_not_bypass_a_flagged_mismatch(cleanup_intents: list[str]):
    """Regression (review finding #1): a `confirm` answer must NOT be a back door
    that commits a value already flagged as a >20% mismatch. The dim stays
    flagged until resolved through the measure_mm re-ask."""
    intent = json.loads(json.dumps(BRACKET_INTENT))
    intent["questions"].append(
        {
            "question_id": "q_confirm_span",
            "dim_name": "span_mm",
            "prompt": "Confirm span is right?",
            "kind": "confirm",
            "choices": None,
            "overlay": None,
        }
    )
    body = _create_intent_with_depth(
        cleanup_intents, depth={"span_mm": 250.0}, intent=intent
    )
    iid = body["intent_id"]

    _answer(iid, "q_span", 25.0)  # flag the mismatch
    # Now a stray confirm for the same dim must not commit it.
    r = client.post(
        f"/intents/{iid}/answers",
        json={"answers": [{"question_id": "q_confirm_span", "confirm": True}]},
    )
    assert r.status_code == 200, r.text
    span = _dim(r.json(), "span_mm")
    assert (
        span["source"] != "user_measured"
    ), "confirm must not commit a flagged mismatch"
    assert span["cross_check"]["status"] == "mismatch_reask"


def test_confirm_disagreeing_with_depth_prior_is_flagged(cleanup_intents: list[str]):
    """Regression (review finding #4): a `confirm` on a value that disagrees with
    the depth prior by >20% must be re-asked, not silently stamped 'ok'. The
    provider is distrusted, so even a provider-supplied cross_check.status='ok'
    on a bad value gets re-checked."""
    intent = json.loads(json.dumps(BRACKET_INTENT))
    # Provider injects a span dim whose value disagrees with its own stated prior.
    span = next(d for d in intent["dimensions"] if d["name"] == "span_mm")
    span["value_mm"] = 500.0
    span["source"] = "depth_inferred"
    span["cross_check"] = {"depth_value_mm": 250.0, "ratio": 2.0, "status": "ok"}
    intent["questions"].append(
        {
            "question_id": "q_confirm_span",
            "dim_name": "span_mm",
            "prompt": "Confirm span is right?",
            "kind": "confirm",
            "choices": None,
            "overlay": None,
        }
    )
    body = _create_intent(
        cleanup_intents, intent=intent
    )  # DEPTH_PROVIDER=none: dim survives as-is
    iid = body["intent_id"]

    r = client.post(
        f"/intents/{iid}/answers",
        json={"answers": [{"question_id": "q_confirm_span", "confirm": True}]},
    )
    assert r.status_code == 200, r.text
    span = _dim(r.json(), "span_mm")
    assert (
        span["source"] != "user_measured"
    ), "confirming a value 2x off the prior must not commit"
    assert span["cross_check"]["status"] == "mismatch_reask"


def test_confirm_agreeing_value_still_commits(cleanup_intents: list[str]):
    """The fix must not break the normal case: confirming a depth-inferred value
    (which equals its prior) commits it."""
    intent = json.loads(json.dumps(BRACKET_INTENT))
    intent["questions"].append(
        {
            "question_id": "q_confirm_span",
            "dim_name": "span_mm",
            "prompt": "Confirm span is right?",
            "kind": "confirm",
            "choices": None,
            "overlay": None,
        }
    )
    body = _create_intent_with_depth(
        cleanup_intents, depth={"span_mm": 250.0}, intent=intent
    )
    r = client.post(
        f"/intents/{body['intent_id']}/answers",
        json={"answers": [{"question_id": "q_confirm_span", "confirm": True}]},
    )
    assert r.status_code == 200, r.text
    span = _dim(r.json(), "span_mm")
    assert span["source"] == "user_measured"
    assert span["value_mm"] == 250.0
    assert span["cross_check"]["status"] == "ok"


def test_crosscheck_never_silently_overrides_user(cleanup_intents: list[str]):
    """The user's number is never replaced by the depth value behind their
    back — a mismatch keeps the user's value while flagged, and commits the
    user's value (not the depth value) on re-confirm."""
    body = _create_intent_with_depth(cleanup_intents, depth={"span_mm": 250.0})
    iid = body["intent_id"]

    flagged = _answer(iid, "q_span", 25.0)
    assert _dim(flagged, "span_mm")["value_mm"] == 25.0  # NOT silently set to 250

    committed = _answer(iid, "q_span", 25.0)
    assert (
        _dim(committed, "span_mm")["value_mm"] == 25.0
    )  # committed as the user's value


# ---------------------------------------------------------------------------
# The intent -> design join (M5): gate, precedence, end-to-end round-trip.
# ---------------------------------------------------------------------------


def _join_intent(cleanup_intents: list[str]) -> str:
    with patch(
        "api.intents.parse_intent", return_value=json.loads(json.dumps(JOIN_VISION))
    ):
        r = client.post("/intents", files=_one_photo_files(), data={"text": "bracket"})
    iid = r.json()["intent_id"]
    cleanup_intents.append(iid)
    return iid


def test_design_gate_409_before_ready(cleanup_intents: list[str]):
    """The critical-dim gate must hold end-to-end: no design until every
    critical dim is user_measured."""
    iid = _join_intent(cleanup_intents)
    r = client.post(f"/intents/{iid}/design")
    assert r.status_code == 409
    assert "not ready_for_design" in r.json()["detail"]


def test_choice_answer_records_chosen_value(cleanup_intents: list[str]):
    """Finish the M3 deferral: a choice answer maps to any enum param (recorded
    as chosen_value on its question)."""
    iid = _join_intent(cleanup_intents)
    updated = client.post(
        f"/intents/{iid}/answers",
        json={"answers": [{"question_id": "q_load", "choice": "heavy"}]},
    ).json()
    q_load = next(q for q in updated["questions"] if q["question_id"] == "q_load")
    assert q_load["chosen_value"] == "heavy"


def test_design_join_precedence_end_to_end(cleanup_intents: list[str]):
    """Full round-trip: the generated params reflect the resolution order —
    user_measured dims, assumed non-critical dim, chosen enum, suggested enum,
    and a default for the rest."""
    iid = _join_intent(cleanup_intents)
    _confirm_all_critical(iid)  # span=200, depth=50 -> user_measured
    client.post(
        f"/intents/{iid}/answers",
        json={"answers": [{"question_id": "q_load", "choice": "heavy"}]},
    )

    r = client.post(f"/intents/{iid}/design")
    assert r.status_code == 200, r.text
    body = r.json()

    assert set(body["files"]) == {"step", "threemf", "stl", "preview_png"}
    by_name = {p["name"]: p for p in body["params"]}

    assert (by_name["span_mm"]["value"], by_name["span_mm"]["source"]) == (
        200.0,
        "measured",
    )
    assert (by_name["depth_mm"]["value"], by_name["depth_mm"]["source"]) == (
        50.0,
        "measured",
    )
    # non-critical dim from the provider's estimate is acceptable
    assert (by_name["thickness_mm"]["value"], by_name["thickness_mm"]["source"]) == (
        5.0,
        "assumed",
    )
    # answered choice beats the provider's suggestion
    assert (by_name["load_hint"]["value"], by_name["load_hint"]["source"]) == (
        "heavy",
        "chosen",
    )
    # unanswered choice falls to the provider's suggested_value
    assert (by_name["screw_size"]["value"], by_name["screw_size"]["source"]) == (
        "#10",
        "suggested",
    )
    # a non-dimension int with no question falls to the template default
    assert (by_name["screw_count"]["value"], by_name["screw_count"]["source"]) == (
        3,
        "default",
    )

    shutil.rmtree(EXPORTS_DIR / body["design_id"], ignore_errors=True)

    # The intent -> design link is persisted.
    stored = client.get(f"/intents/{iid}").json()
    assert stored["design_id"] == body["design_id"]
    assert "design_files" in stored


def test_design_downloads_are_fetchable(cleanup_intents: list[str]):
    iid = _join_intent(cleanup_intents)
    _confirm_all_critical(iid)
    body = client.post(f"/intents/{iid}/design").json()
    for url in body["files"].values():
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert len(resp.content) > 0
    # cleanup the exported files for this design
    shutil.rmtree(
        INTENTS_DIR.parent.parent / "exports" / body["design_id"], ignore_errors=True
    )


def test_design_refuses_critical_dim_not_user_measured(cleanup_intents: list[str]):
    """Defensive gate inside the join: even a (hand-tampered) intent marked
    ready_for_design must not build if a critical dim isn't user_measured."""
    tampered = json.loads(json.dumps(JOIN_VISION))
    tampered["intent_id"] = "tampered1234"
    tampered["status"] = "ready_for_design"  # lie
    for d in tampered["dimensions"]:
        if d["name"] == "span_mm":
            d["source"] = "depth_inferred"  # a critical dim NOT user_measured
        elif d["name"] == "depth_mm":
            d["source"] = "user_measured"
    INTENTS_DIR.mkdir(parents=True, exist_ok=True)
    (INTENTS_DIR / "tampered1234.json").write_text(json.dumps(tampered))
    cleanup_intents.append("tampered1234")

    r = client.post("/intents/tampered1234/design")
    assert r.status_code == 409
    assert "span_mm" in r.json()["detail"]


def test_gate_requires_a_value_not_just_source(cleanup_intents: list[str]):
    """Regression (M5 review #1): a critical dim marked source="user_measured"
    but with value_mm=None must NOT open the ready_for_design gate."""
    tampered = json.loads(json.dumps(BRACKET_INTENT))
    for d in tampered["dimensions"]:
        if d["name"] == "span_mm":
            d["source"], d["value_mm"] = (
                "user_measured",
                None,
            )  # confirmed but valueless
        elif d["name"] == "depth_mm":
            d["source"], d["value_mm"] = "user_measured", 40.0
    body = _create_intent(cleanup_intents, intent=tampered)
    assert body["status"] == "needs_answers"


def test_design_refuses_valueless_critical_dim(cleanup_intents: list[str]):
    """Regression (M5 review #1): even a (tampered) ready_for_design intent whose
    critical dim has no value must 409 in the join — never fall through to the
    template default and build a part with a fabricated fit-critical dimension."""
    tampered = json.loads(json.dumps(JOIN_VISION))
    tampered["intent_id"] = "valueless99"
    tampered["status"] = "ready_for_design"
    for d in tampered["dimensions"]:
        if d["name"] == "span_mm":
            d["source"], d["value_mm"] = "user_measured", None
        elif d["name"] == "depth_mm":
            d["source"], d["value_mm"] = "user_measured", 50.0
    INTENTS_DIR.mkdir(parents=True, exist_ok=True)
    (INTENTS_DIR / "valueless99.json").write_text(json.dumps(tampered))
    cleanup_intents.append("valueless99")

    r = client.post("/intents/valueless99/design")
    assert r.status_code == 409
    assert "span_mm" in r.json()["detail"]


def _measure_q(qid: str, dim: str) -> dict:
    return {
        "question_id": qid,
        "dim_name": dim,
        "prompt": f"{dim}?",
        "kind": "measure_mm",
        "choices": None,
        "overlay": None,
        "suggested_value": None,
        "chosen_value": None,
    }


def test_measure_question_without_dimension_is_answerable(cleanup_intents: list[str]):
    """Regression: a provider that emits a measure_mm question whose dim_name is
    missing from dimensions[] must NOT dead-end the user with a 422. The gate
    synthesizes the dimension at creation, and answering commits the value."""
    variant = json.loads(json.dumps(BRACKET_INTENT))
    variant["questions"].append(_measure_q("q_gap", "gap_wall_to_tap_mm"))

    body = _create_intent(cleanup_intents, intent=variant)
    # The gate synthesized the missing (non-critical) dimension.
    gap = next(
        (d for d in body["dimensions"] if d["name"] == "gap_wall_to_tap_mm"), None
    )
    assert gap is not None and gap["critical"] is False

    updated = client.post(
        f"/intents/{body['intent_id']}/answers",
        json={"answers": [{"question_id": "q_gap", "measure_mm": 37.5}]},
    )
    assert updated.status_code == 200, updated.text
    gap = next(
        d for d in updated.json()["dimensions"] if d["name"] == "gap_wall_to_tap_mm"
    )
    assert gap["value_mm"] == 37.5 and gap["source"] == "user_measured"


def test_derive_dim_name():
    from api.intents import _derive_dim_name

    assert _derive_dim_name("q_wall_to_faucet_center") == "wall_to_faucet_center_mm"
    assert _derive_dim_name("reask-span_mm") == "span_mm"
    assert _derive_dim_name("auto-shaft_dia_mm") == "shaft_dia_mm"
    assert _derive_dim_name("Q_Weird Name!!") == "weird_name_mm"
    assert _derive_dim_name(None) == "measurement_mm"
    assert _derive_dim_name("") == "measurement_mm"


def test_measure_question_with_null_dim_name_is_answerable(cleanup_intents: list[str]):
    """Regression: a measure_mm question the provider invented with NO dim_name
    (e.g. 'q_wall_to_faucet_center') must still be answerable — the gate derives
    a dimension name from the question id and creates a backing dimension."""
    variant = json.loads(json.dumps(BRACKET_INTENT))
    q = _measure_q("q_wall_to_faucet_center", "unused")
    q["dim_name"] = None  # provider left it null
    variant["questions"].append(q)

    body = _create_intent(cleanup_intents, intent=variant)
    derived = "wall_to_faucet_center_mm"
    assert any(d["name"] == derived for d in body["dimensions"])
    q_out = next(
        q for q in body["questions"] if q["question_id"] == "q_wall_to_faucet_center"
    )
    assert q_out["dim_name"] == derived  # persisted onto the question

    updated = client.post(
        f"/intents/{body['intent_id']}/answers",
        json={
            "answers": [{"question_id": "q_wall_to_faucet_center", "measure_mm": 150.0}]
        },
    )
    assert updated.status_code == 200, updated.text
    dim = next(d for d in updated.json()["dimensions"] if d["name"] == derived)
    assert dim["value_mm"] == 150.0 and dim["source"] == "user_measured"


def test_stale_intent_missing_dim_is_answerable(cleanup_intents: list[str]):
    """Defensive: even a persisted intent whose measure question predates the
    gate normalization (no backing dimension) is answerable, not a 422 —
    _apply_answer creates the dimension on demand."""
    stale = json.loads(json.dumps(BRACKET_INTENT))
    stale["intent_id"] = "staledim01"
    stale["questions"].append(_measure_q("q_orphan", "orphan_mm"))
    # Ensure there is NO backing dimension for orphan_mm.
    assert not any(d["name"] == "orphan_mm" for d in stale["dimensions"])
    INTENTS_DIR.mkdir(parents=True, exist_ok=True)
    (INTENTS_DIR / "staledim01.json").write_text(json.dumps(stale))
    cleanup_intents.append("staledim01")

    r = client.post(
        "/intents/staledim01/answers",
        json={"answers": [{"question_id": "q_orphan", "measure_mm": 12.0}]},
    )
    assert r.status_code == 200, r.text
    orphan = next(d for d in r.json()["dimensions"] if d["name"] == "orphan_mm")
    assert orphan["value_mm"] == 12.0 and orphan["source"] == "user_measured"


# ---------------------------------------------------------------------------
# M5.5: photo persistence + the in-photo ghost composite
# ---------------------------------------------------------------------------


def test_photos_and_annotation_persisted(cleanup_intents: list[str]):
    """Uploaded photos are written under data/intents/<id>/photos/ and the raw
    annotation is stored on the intent, so the design join can build a ghost."""
    photo = _valid_photo_files()
    photo_bytes = photo[0][1][1].getvalue()
    annotation = [{"photo_index": 0, "points": [[0.3, 0.4], [0.5, 0.6]]}]
    with patch(
        "api.intents.parse_intent", return_value=json.loads(json.dumps(JOIN_VISION))
    ):
        body = client.post(
            "/intents",
            files=photo,
            data={"text": "bracket", "annotation": json.dumps(annotation)},
        ).json()
    iid = body["intent_id"]
    cleanup_intents.append(iid)

    assert body["annotation"] == annotation
    assert body["photos"] and body["photos"][0]["index"] == 0

    on_disk = DATA_DIR / body["photos"][0]["path"]
    assert on_disk.exists()
    assert on_disk.read_bytes() == photo_bytes
    # Stored under the intent's own directory.
    assert on_disk.parent == INTENTS_DIR / iid / "photos"


def _ready_intent_with_valid_photo(
    cleanup_intents: list[str], annotation: list | None
) -> str:
    data = {"text": "bracket"}
    if annotation is not None:
        data["annotation"] = json.dumps(annotation)
    with patch(
        "api.intents.parse_intent", return_value=json.loads(json.dumps(JOIN_VISION))
    ):
        r = client.post("/intents", files=_valid_photo_files(), data=data)
    iid = r.json()["intent_id"]
    cleanup_intents.append(iid)
    _confirm_all_critical(iid)
    return iid


def test_join_produces_fetchable_composite(cleanup_intents: list[str]):
    """With a stored (decodable) photo, the join returns files.composite and it
    downloads as a non-empty image."""
    annotation = [{"photo_index": 0, "points": [[0.4, 0.5], [0.6, 0.62]]}]
    iid = _ready_intent_with_valid_photo(cleanup_intents, annotation)
    body = client.post(f"/intents/{iid}/design").json()
    try:
        assert "composite" in body["files"], body["files"]
        resp = client.get(body["files"]["composite"])
        assert resp.status_code == 200
        assert len(resp.content) > 0
    finally:
        shutil.rmtree(EXPORTS_DIR / body["design_id"], ignore_errors=True)


def test_join_without_photo_has_no_composite(cleanup_intents: list[str]):
    """Absent a stored photo, the join still succeeds and simply omits the
    composite (no error) — the part files are the real deliverable."""
    iid = _ready_intent_with_valid_photo(cleanup_intents, annotation=None)

    # Simulate an intent that has no stored photo (e.g. an older intent).
    path = INTENTS_DIR / f"{iid}.json"
    stored = json.loads(path.read_text())
    stored["photos"] = []
    path.write_text(json.dumps(stored))

    body = client.post(f"/intents/{iid}/design").json()
    try:
        assert "composite" not in body["files"]
        assert set(body["files"]) == {"step", "threemf", "stl", "preview_png"}
    finally:
        shutil.rmtree(EXPORTS_DIR / body["design_id"], ignore_errors=True)


def test_bounded_depth_returns_value_when_fast():
    from api.intents import _bounded_depth_mm_at
    from api.photo import PhotoInput

    with patch("api.intents.depth_mm_at", return_value=123.4):
        got = _bounded_depth_mm_at(PhotoInput(content=b"x"), 0.5, 0.5, deadline_s=2.0)
    assert got == 123.4


def test_bounded_depth_times_out_and_returns_none():
    """M5.5 review: a slow (replicate) depth lookup must never block the join —
    the bounded helper returns None once the deadline passes."""
    import time

    from api.intents import _bounded_depth_mm_at
    from api.photo import PhotoInput

    def slow(*_a, **_k):
        time.sleep(1.0)
        return 999.0

    start = time.monotonic()
    with patch("api.intents.depth_mm_at", side_effect=slow):
        got = _bounded_depth_mm_at(PhotoInput(content=b"x"), 0.5, 0.5, deadline_s=0.1)
    elapsed = time.monotonic() - start
    assert got is None
    assert elapsed < 0.9, f"bounded depth waited too long ({elapsed:.2f}s)"
