"""API-level tests: POST /designs round-trip via httpx (FastAPI TestClient),
param validation errors, unknown template handling, GET /templates."""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path
from typing import Iterator

import cadquery as cq
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import templates_lib.registry as registry
from api.designs import EXPORTS_DIR, build_design
from api.main import app
from templates_lib.registry import all_templates, get_template

client = TestClient(app)

TEMPLATE_IDS = sorted(all_templates())

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


def test_manifold_gate_rejects_non_watertight_mesh_and_cleans_up(monkeypatch):
    """M5.5 runtime manifold gate: if a template builds a non-watertight solid,
    build_design must refuse it with a 500 and leave NO export directory behind
    (fail closed — no unprintable STL/STEP can ever be downloaded)."""
    spec = get_template("bracket_shelf_l")

    # A single planar face exports to an open, 2-triangle, NON-watertight mesh.
    def broken_build(_params):
        return cq.Workplane(obj=cq.Face.makePlane(10, 10))

    broken_spec = dataclasses.replace(spec, build_fn=broken_build)
    monkeypatch.setitem(registry._REGISTRY, "bracket_shelf_l", broken_spec)

    def dirs() -> set[str]:
        return (
            {p.name for p in EXPORTS_DIR.iterdir()} if EXPORTS_DIR.exists() else set()
        )

    before = dirs()
    with pytest.raises(HTTPException) as exc:
        build_design("bracket_shelf_l", {"span_mm": 120, "depth_mm": 40})
    assert exc.value.status_code == 500
    assert "watertight" in exc.value.detail.lower()
    assert dirs() == before, "a rejected design left its export directory behind"


def test_templates_endpoint_lists_all_registered_templates():
    response = client.get("/templates")
    assert response.status_code == 200
    body = response.json()

    assert {t["template_id"] for t in body} == set(TEMPLATE_IDS)
    for template in body:
        assert template["fields"], f"{template['template_id']} has no fields"
        for field in template["fields"]:
            assert field["type"] in ("number", "integer", "boolean", "choice")
            assert field["default"] is not None


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_designs_round_trip_for_every_template(
    cleanup_design_dirs: list[Path], template_id: str
):
    """Every template's params model has full defaults (see
    templates_lib/registry.py), so an empty params dict is itself a valid
    request — this exercises the full pipeline for every registered
    template, not just bracket_shelf_l."""
    response = client.post("/designs", json={"template_id": template_id, "params": {}})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["template_id"] == template_id
    cleanup_design_dirs.append(EXPORTS_DIR / body["design_id"])

    for url in body["files"].values():
        file_response = client.get(url)
        assert file_response.status_code == 200, url
        assert len(file_response.content) > 0
