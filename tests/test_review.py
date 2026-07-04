"""End-to-end Track B tests via the HTTP API: the mocked freeform round trip
(request → generated template → measured dims → design → pending_review → approve
→ files) and the founder review + download gate. Vision and codegen are mocked;
the sandbox runs for real.
"""

from __future__ import annotations

import io
import shutil
from typing import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import api.design_store as ds
import api.freeform as ff
from api.designs import EXPORTS_DIR
from api.intents import INTENTS_DIR
from api.main import app

client = TestClient(app)

OTHER_INTENT = {
    "intent_id": "x",
    "status": "needs_answers",
    "category": "other",
    "template_id": None,
    "description": "A custom bridging plate.",
    "context_notes": "",
    "material_suggestion": "PETG",
    "out_of_scope_reason": None,
    "dimensions": [],
    "questions": [],
}
GOOD_GEN = {
    "cadquery_code": (
        "import cadquery as cq\n"
        "def build(params):\n"
        "    return cq.Workplane('XY').box(params['span_mm'], params['width_mm'], params['thickness_mm'])\n"
    ),
    "param_schema": [
        {
            "name": "span_mm",
            "type": "number",
            "default": 220,
            "minimum": 50,
            "maximum": 249,
            "choices": None,
            "description": "span",
        },
        {
            "name": "width_mm",
            "type": "number",
            "default": 40,
            "minimum": 10,
            "maximum": 100,
            "choices": None,
            "description": "width",
        },
        {
            "name": "thickness_mm",
            "type": "number",
            "default": 6,
            "minimum": 3,
            "maximum": 20,
            "choices": None,
            "description": "thickness",
        },
    ],
    "assumptions": ["Assumed a flat rectangular plate."],
    "critical_dims": ["span_mm", "width_mm"],
}
UNSAFE_GEN = {
    **GOOD_GEN,
    "cadquery_code": "import os\ndef build(params):\n    return None\n",
}


@pytest.fixture
def cleanup() -> Iterator[dict]:
    tracked = {"intents": [], "designs": [], "generated": []}
    yield tracked
    for iid in tracked["intents"]:
        (INTENTS_DIR / f"{iid}.json").unlink(missing_ok=True)
        shutil.rmtree(INTENTS_DIR / iid, ignore_errors=True)
    for did in tracked["designs"]:
        shutil.rmtree(EXPORTS_DIR / did, ignore_errors=True)
        (ds.DESIGNS_DIR / f"{did}.json").unlink(missing_ok=True)
    for gid in tracked["generated"]:
        shutil.rmtree(ff.GENERATED_DIR / gid, ignore_errors=True)
    if INTENTS_DIR.exists() and not any(INTENTS_DIR.iterdir()):
        shutil.rmtree(INTENTS_DIR.parent, ignore_errors=True)


def _photo() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 200, 200)).save(buf, "JPEG")
    return buf.getvalue()


def _make_other_intent(cleanup: dict) -> str:
    with patch("api.intents.parse_intent", return_value=dict(OTHER_INTENT)):
        r = client.post(
            "/intents",
            files=[("photos", ("p.jpg", io.BytesIO(_photo()), "image/jpeg"))],
            data={"text": "a flat bridge plate spanning two shelves 220mm apart"},
        )
    body = r.json()
    cleanup["intents"].append(body["intent_id"])
    assert body["freeform_available"] is True and body["template_id"] is None
    return body["intent_id"]


def _generate(cleanup: dict, iid: str, gen=GOOD_GEN) -> dict:
    with patch(
        "api.freeform.codegen_provider.generate_template", return_value=dict(gen)
    ), patch("api.intents.codegen_provider.check_provider_configured"):
        r = client.post(f"/intents/{iid}/freeform")
    body = r.json()
    if body.get("template_id"):
        cleanup["generated"].append(body["template_id"])
    return body


def test_freeform_end_to_end_and_review_gate(cleanup):
    iid = _make_other_intent(cleanup)

    intent = _generate(cleanup, iid)
    assert intent["template_id"].startswith("gen_") and intent["freeform"] is True
    measure_qs = [
        q["question_id"] for q in intent["questions"] if q["kind"] == "measure_mm"
    ]
    assert set(q.split("-")[-1] for q in measure_qs) >= {"span_mm", "width_mm"}

    answers = [
        {"question_id": q, "measure_mm": 220.0 if "span" in q else 40.0}
        for q in measure_qs
    ]
    ready = client.post(f"/intents/{iid}/answers", json={"answers": answers}).json()
    assert ready["status"] == "ready_for_design"

    design = client.post(f"/intents/{iid}/design").json()
    did = design["design_id"]
    cleanup["designs"].append(did)
    assert design["review_status"] == "pending_review" and design["freeform"] is True

    # Review queue lists it.
    pending = client.get("/review").json()
    assert any(r["design_id"] == did for r in pending)

    # Gate: CAD downloads blocked, preview allowed.
    assert client.get(f"/exports/{did}/part.stl").status_code == 403
    assert client.get(f"/exports/{did}/part.step").status_code == 403
    assert client.get(f"/exports/{did}/part.3mf").status_code == 403
    assert client.get(f"/exports/{did}/preview.png").status_code == 200

    # Approve -> downloads unlock.
    approved = client.post(
        f"/review/{did}", json={"verdict": "approve", "note": "ship it"}
    ).json()
    assert approved["status"] == "approved" and approved["review_note"] == "ship it"
    resp = client.get(f"/exports/{did}/part.stl")
    assert resp.status_code == 200 and len(resp.content) > 0


def test_reject_keeps_downloads_locked(cleanup):
    iid = _make_other_intent(cleanup)
    intent = _generate(cleanup, iid)
    measure_qs = [
        q["question_id"] for q in intent["questions"] if q["kind"] == "measure_mm"
    ]
    answers = [
        {"question_id": q, "measure_mm": 220.0 if "span" in q else 40.0}
        for q in measure_qs
    ]
    client.post(f"/intents/{iid}/answers", json={"answers": answers})
    did = client.post(f"/intents/{iid}/design").json()["design_id"]
    cleanup["designs"].append(did)

    rejected = client.post(
        f"/review/{did}", json={"verdict": "reject", "note": "walls too thin"}
    ).json()
    assert rejected["status"] == "rejected"
    assert client.get(f"/exports/{did}/part.stl").status_code == 403


def test_freeform_generation_failure_returns_error(cleanup):
    iid = _make_other_intent(cleanup)
    intent = _generate(cleanup, iid, gen=UNSAFE_GEN)
    assert intent.get("template_id") is None
    assert intent.get("freeform_error")
    assert intent.get("freeform_available") is False


def test_freeform_override_replaces_matched_template(cleanup):
    # Part A: freeform is the always-available user override — even when a
    # template matched, "Design this custom instead" replaces it with a
    # generated one (and clears the old template's questions/dims).
    bracket = dict(OTHER_INTENT, template_id="bracket_shelf_l", category="bracket")
    with patch("api.intents.parse_intent", return_value=bracket):
        r = client.post(
            "/intents",
            files=[("photos", ("p.jpg", io.BytesIO(_photo()), "image/jpeg"))],
            data={"text": "a bracket, but actually custom"},
        )
    iid = r.json()["intent_id"]
    cleanup["intents"].append(iid)

    overridden = _generate(cleanup, iid)  # POST /intents/{id}/freeform
    assert overridden["template_id"].startswith("gen_")  # replaced the bracket
    assert overridden["freeform"] is True
    # Only the generated template's critical dims remain (old bracket dims gone).
    assert {
        q["dim_name"] for q in overridden["questions"] if q["kind"] == "measure_mm"
    } == {
        "span_mm",
        "width_mm",
    }


def test_review_404_for_unknown_design():
    assert client.get("/review/nope").status_code == 404
    assert client.post("/review/nope", json={"verdict": "approve"}).status_code == 404


def test_invalid_verdict_rejected(cleanup):
    iid = _make_other_intent(cleanup)
    intent = _generate(cleanup, iid)
    measure_qs = [
        q["question_id"] for q in intent["questions"] if q["kind"] == "measure_mm"
    ]
    answers = [
        {"question_id": q, "measure_mm": 220.0 if "span" in q else 40.0}
        for q in measure_qs
    ]
    client.post(f"/intents/{iid}/answers", json={"answers": answers})
    did = client.post(f"/intents/{iid}/design").json()["design_id"]
    cleanup["designs"].append(did)
    assert client.post(f"/review/{did}", json={"verdict": "maybe"}).status_code == 422


def _pending_freeform_design(cleanup) -> str:
    iid = _make_other_intent(cleanup)
    intent = _generate(cleanup, iid)
    qs = [q["question_id"] for q in intent["questions"] if q["kind"] == "measure_mm"]
    client.post(
        f"/intents/{iid}/answers",
        json={
            "answers": [
                {"question_id": q, "measure_mm": 220.0 if "span" in q else 40.0}
                for q in qs
            ]
        },
    )
    did = client.post(f"/intents/{iid}/design").json()["design_id"]
    cleanup["designs"].append(did)
    return did


def test_gate_not_bypassable_by_casing_or_noncanonical_paths(cleanup):
    did = _pending_freeform_design(cleanup)
    # Canonical is 403 (baseline).
    assert client.get(f"/exports/{did}/part.stl").status_code == 403
    # Casing must ALSO be blocked (case-insensitive FS would resolve PART.STL).
    assert client.get(f"/exports/{did}/PART.STL").status_code in (403, 404)
    assert client.get(f"/exports/{did}/Part.Stl").status_code in (403, 404)
    # Non-canonical shapes must not serve the gated file (404, never 200).
    for path in (
        f"/exports/{did}/part.stl/",
        f"/exports/{did}//part.stl",
        f"/exports/{did}/./part.stl",
    ):
        assert client.get(path).status_code != 200, path


def test_review_token_enforced_when_set(cleanup, monkeypatch):
    did = _pending_freeform_design(cleanup)
    monkeypatch.setenv("VULCAN_REVIEW_TOKEN", "s3cret")
    # No / wrong token -> 403; downloads stay locked.
    assert client.post(f"/review/{did}", json={"verdict": "approve"}).status_code == 403
    assert (
        client.post(
            f"/review/{did}",
            json={"verdict": "approve"},
            headers={"X-Review-Token": "wrong"},
        ).status_code
        == 403
    )
    assert client.get(f"/exports/{did}/part.stl").status_code == 403
    # Correct token approves.
    ok = client.post(
        f"/review/{did}",
        json={"verdict": "approve"},
        headers={"X-Review-Token": "s3cret"},
    )
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert client.get(f"/exports/{did}/part.stl").status_code == 200


def test_track_a_downloads_are_not_gated(cleanup):
    # A direct Track A design has no review record -> served freely (unchanged).
    payload = {"template_id": "bracket_shelf_l", "params": {}}
    body = client.post("/designs", json=payload).json()
    did = body["design_id"]
    cleanup["designs"].append(did)
    assert client.get(f"/exports/{did}/part.stl").status_code == 200
