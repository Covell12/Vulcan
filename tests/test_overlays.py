"""M7 Part B: dimension-line overlay schema. The new overlay kinds (dim_line,
dim_ellipse, dim_depth) must validate against the IntentSpec schema, and an
intent whose (mocked) provider emits them must round-trip through POST /intents
with the overlays intact — a JS-free sanity check that the API emits valid
new-kind overlays.
"""

from __future__ import annotations

import io
import shutil
from typing import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api.intents import INTENTS_DIR, _validation_errors
from api.main import app

client = TestClient(app)


def _q(qid, dim, overlay):
    return {
        "question_id": qid,
        "dim_name": dim,
        "prompt": f"measure {dim}?",
        "kind": "measure_mm",
        "choices": None,
        "overlay": overlay,
        "suggested_value": None,
        "chosen_value": None,
    }


def _dim(name):
    return {
        "name": name,
        "value_mm": None,
        "source": "assumed",
        "confidence": 0.3,
        "critical": True,
        "cross_check": None,
    }


OVERLAY_INTENT = {
    "intent_id": "x",
    "status": "needs_answers",
    "category": "adapter",
    "template_id": "adapter_tube",
    "description": "a tube adapter",
    "context_notes": "",
    "material_suggestion": "PETG",
    "out_of_scope_reason": None,
    "template_fit": 0.8,
    "unsupported_features": [],
    "dimensions": [_dim("od_a_mm"), _dim("id_a_mm"), _dim("od_b_mm"), _dim("id_b_mm")],
    "questions": [
        # a diameter on a round thing MUST use dim_ellipse
        _q(
            "q_od_a",
            "od_a_mm",
            {
                "photo_index": 0,
                "kind": "dim_ellipse",
                "center": [0.3, 0.5],
                "rx": 0.12,
                "ry": 0.05,
                "rotation": 8,
            },
        ),
        _q(
            "q_id_a",
            "id_a_mm",
            {
                "photo_index": 0,
                "kind": "dim_ellipse",
                "center": [0.3, 0.5],
                "rx": 0.07,
                "ry": 0.03,
                "rotation": 8,
            },
        ),
        # a straight linear measurement
        _q(
            "q_od_b",
            "od_b_mm",
            {"photo_index": 0, "kind": "dim_line", "points": [[0.6, 0.4], [0.6, 0.6]]},
        ),
        # a receding measurement
        _q(
            "q_id_b",
            "id_b_mm",
            {
                "photo_index": 0,
                "kind": "dim_depth",
                "points": [[0.62, 0.5], [0.8, 0.45]],
            },
        ),
    ],
}


def test_new_overlay_kinds_validate_against_schema():
    assert _validation_errors(OVERLAY_INTENT) == []


@pytest.mark.parametrize(
    "overlay",
    [
        {"photo_index": 0, "kind": "dim_line", "points": [[0.1, 0.1], [0.2, 0.2]]},
        {"photo_index": 0, "kind": "dim_depth", "points": [[0.1, 0.1], [0.3, 0.2]]},
        {
            "photo_index": 0,
            "kind": "dim_ellipse",
            "center": [0.5, 0.5],
            "rx": 0.1,
            "ry": 0.06,
            "rotation": 0,
        },
        # legacy shapes still accepted for backward compatibility
        {"photo_index": 0, "shape": "circle", "points": [[0.5, 0.5]]},
        {"photo_index": 0, "shape": "arrow", "points": [[0.1, 0.1], [0.2, 0.2]]},
        None,
    ],
)
def test_overlay_kind_variants_validate(overlay):
    spec = {
        "intent_id": "x",
        "status": "needs_answers",
        "category": "other",
        "description": "d",
        "dimensions": [_dim("d_mm")],
        "questions": [_q("q1", "d_mm", overlay)],
    }
    assert _validation_errors(spec) == []


def test_bad_overlay_kind_rejected():
    spec = {
        "intent_id": "x",
        "status": "needs_answers",
        "category": "other",
        "description": "d",
        "dimensions": [_dim("d_mm")],
        "questions": [_q("q1", "d_mm", {"photo_index": 0, "kind": "not_a_kind"})],
    }
    assert _validation_errors(spec)  # enum violation


@pytest.fixture
def cleanup_intents() -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    for iid in created:
        (INTENTS_DIR / f"{iid}.json").unlink(missing_ok=True)
        shutil.rmtree(INTENTS_DIR / iid, ignore_errors=True)


def test_api_emits_valid_new_kind_overlays(cleanup_intents):
    """A mocked provider that emits the new overlay kinds -> POST /intents 200 and
    the overlays survive to the response (post-processing doesn't drop/break them)."""
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 200, 200)).save(buf, "JPEG")
    with patch("api.intents.parse_intent", return_value=dict(OVERLAY_INTENT)):
        r = client.post(
            "/intents",
            files=[("photos", ("p.jpg", io.BytesIO(buf.getvalue()), "image/jpeg"))],
            data={"text": "a tube adapter"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    cleanup_intents.append(body["intent_id"])
    kinds = {q["overlay"]["kind"] for q in body["questions"] if q.get("overlay")}
    assert kinds == {"dim_ellipse", "dim_line", "dim_depth"}
    # and it's still schema-valid as persisted/returned
    assert _validation_errors(body) == []
