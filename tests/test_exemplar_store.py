"""Tests for api/exemplar_store.py — the approved-design few-shot memory.

Embeddings use the deterministic LOCAL provider (set in conftest), so retrieval
ordering is reproducible with no network/key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api import exemplar_store


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(exemplar_store, "EXEMPLARS_DIR", tmp_path / "exemplars")
    return exemplar_store


def _schema(name: str) -> list[dict]:
    return [{"name": name, "type": "number", "default": 10}]


def test_empty_store_returns_no_exemplars(store):
    assert store.retrieve("anything", k=2) == []
    assert store.count() == 0


def test_retrieval_orders_by_similarity(store):
    store.add_exemplar(
        "a bridge plate to span the gap between two shelves",
        _schema("span_mm"),
        "code_bridge",
    )
    store.add_exemplar("a round knob for an oven dial", _schema("dia_mm"), "code_knob")
    store.add_exemplar(
        "a hook to hang a coat on the wall", _schema("reach_mm"), "code_hook"
    )
    top = store.retrieve("a bridge that spans a gap between two shelves", k=2)
    assert len(top) == 2
    # The bridge exemplar is the most similar and must rank first.
    assert top[0]["code"] == "code_bridge"
    assert top[0]["similarity"] >= top[1]["similarity"]
    # Every returned exemplar carries its request/schema/code for few-shot use.
    assert top[0]["param_schema"] == _schema("span_mm")


def test_add_is_idempotent_per_design_id(store):
    store.add_exemplar("a widget", _schema("w_mm"), "v1", design_id="d1")
    store.add_exemplar("a widget", _schema("w_mm"), "v2", design_id="d1")
    assert store.count() == 1
    top = store.retrieve("a widget", k=5)
    assert len(top) == 1 and top[0]["code"] == "v2"
