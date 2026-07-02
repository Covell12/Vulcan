"""API-level tests: POST /designs round-trip via httpx (FastAPI TestClient),
param validation errors, unknown template handling."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from api.designs import EXPORTS_DIR
from api.main import app

client = TestClient(app)

VALID_PAYLOAD = {
    "template_id": "bracket_shelf_l",
    "params": {
        "span_mm": 120,
        "depth_mm": 40,
        "thickness_mm": 4,
        "screw_size": "#8",
        "screw_count": 3,
        "load_hint": "medium",
    },
}


@pytest.fixture
def cleanup_design_dirs() -> Iterator[list[Path]]:
    created: list[Path] = []
    yield created
    for design_dir in created:
        shutil.rmtree(design_dir, ignore_errors=True)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_designs_round_trip(cleanup_design_dirs: list[Path]):
    response = client.post("/designs", json=VALID_PAYLOAD)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["template_id"] == "bracket_shelf_l"
    assert set(body["files"]) == {"step", "threemf", "stl", "preview_png"}

    cleanup_design_dirs.append(EXPORTS_DIR / body["design_id"])

    for url in body["files"].values():
        file_response = client.get(url)
        assert file_response.status_code == 200, url
        assert len(file_response.content) > 0


def test_designs_invalid_params_returns_422(cleanup_design_dirs: list[Path]):
    payload = {**VALID_PAYLOAD, "params": {**VALID_PAYLOAD["params"], "span_mm": 5}}
    response = client.post("/designs", json=payload)
    assert response.status_code == 422


def test_designs_geometry_conflict_returns_422(cleanup_design_dirs: list[Path]):
    payload = {
        "template_id": "bracket_shelf_l",
        "params": {
            "span_mm": 45,
            "depth_mm": 40,
            "thickness_mm": 4,
            "screw_size": "#10",
            "screw_count": 6,
            "load_hint": "medium",
        },
    }
    response = client.post("/designs", json=payload)
    assert response.status_code == 422


def test_designs_unknown_template_returns_400():
    response = client.post("/designs", json={"template_id": "nope", "params": {}})
    assert response.status_code == 400
