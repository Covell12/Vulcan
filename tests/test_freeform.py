"""Tests for api/freeform.py — the Track B generation orchestration. The codegen
provider is mocked throughout (no network); the sandbox runs for real (it's the
containment boundary and must be exercised)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

import api.freeform as ff
from templates_lib.registry import EphemeralTemplateSpec, get_template

GOOD_GEN = {
    "cadquery_code": (
        "import cadquery as cq\n"
        "def build(params):\n"
        "    return cq.Workplane('XY').box(params['w_mm'], params['d_mm'], params['h_mm'])\n"
    ),
    "param_schema": [
        {
            "name": "w_mm",
            "type": "number",
            "default": 40,
            "minimum": 10,
            "maximum": 200,
            "choices": None,
            "description": "width",
        },
        {
            "name": "d_mm",
            "type": "number",
            "default": 30,
            "minimum": 10,
            "maximum": 200,
            "choices": None,
            "description": "depth",
        },
        {
            "name": "h_mm",
            "type": "number",
            "default": 20,
            "minimum": 5,
            "maximum": 200,
            "choices": None,
            "description": "height",
        },
    ],
    "assumptions": ["assumed a solid block"],
    "critical_dims": ["w_mm", "d_mm"],
}
UNSAFE_GEN = {
    **GOOD_GEN,
    "cadquery_code": "import os\ndef build(params):\n    return None\n",
}
BADBUILD_GEN = {
    **GOOD_GEN,
    "cadquery_code": "import cadquery as cq\ndef build(params):\n    return cq.Workplane('XY').box(0,0,0)\n",
}
OVERSIZE_GEN = {
    **GOOD_GEN,
    "cadquery_code": "import cadquery as cq\ndef build(params):\n    return cq.Workplane('XY').box(400, 30, 20)\n",
}
# Two disjoint boxes: each is watertight, so the manifold check passes, but the
# part is TWO connected bodies (floating pieces) — must be caught + rejected.
FLOATING_GEN = {
    **GOOD_GEN,
    "cadquery_code": (
        "import cadquery as cq\n"
        "def build(params):\n"
        "    a = cq.Workplane('XY').box(10, 10, 10)\n"
        "    b = cq.Workplane('XY').box(10, 10, 10).translate((40, 0, 0))\n"
        "    return a.union(b)\n"
    ),
}


@pytest.fixture
def cleanup_generated() -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    for gen_id in created:
        shutil.rmtree(ff.GENERATED_DIR / gen_id, ignore_errors=True)


# --- schema normalization + model synthesis ------------------------------


def test_normalize_param_schema_coerces_and_labels():
    raw = [
        {
            "name": "span_mm",
            "type": "number",
            "default": "220",
            "minimum": 50,
            "maximum": 249,
            "description": "d",
        },
        {
            "name": "count",
            "type": "integer",
            "default": 3.0,
            "minimum": 1,
            "maximum": 8,
            "description": "",
        },
        {
            "name": "style",
            "type": "choice",
            "default": "x",
            "choices": ["a", "b"],
            "description": "",
        },
    ]
    fields = ff.normalize_param_schema(raw)
    by = {f["name"]: f for f in fields}
    assert by["span_mm"]["default"] == 220.0 and by["span_mm"]["label"] == "Span (mm)"
    assert by["count"]["default"] == 3 and by["count"]["type"] == "integer"
    # choice default not in choices -> snapped to first choice
    assert by["style"]["default"] == "a" and by["style"]["choices"] == ["a", "b"]


def test_choice_param_without_choices_rejected():
    with pytest.raises(ff.GenerationRejected):
        ff.normalize_param_schema(
            [{"name": "s", "type": "choice", "default": "x", "choices": []}]
        )


def test_build_params_model_enforces_bounds():
    fields = ff.normalize_param_schema(GOOD_GEN["param_schema"])
    Model = ff.build_params_model(fields)
    assert Model().model_dump() == {"w_mm": 40.0, "d_mm": 30.0, "h_mm": 20.0}
    with pytest.raises(Exception):
        Model(w_mm=5)  # below minimum 10


# --- the self-repair loop ------------------------------------------------


def test_generate_success_first_try(cleanup_generated):
    with patch(
        "api.freeform.codegen_provider.generate_template", return_value=dict(GOOD_GEN)
    ):
        r = ff.generate_and_register("a block", [])
    cleanup_generated.append(r.template_id)
    assert r.ok and r.template_id.startswith("gen_")
    assert r.dfm["manifold"] and r.dfm["within_size"]
    spec = get_template(r.template_id)
    assert isinstance(spec, EphemeralTemplateSpec)
    assert spec.critical_dims == ("w_mm", "d_mm")
    assert (ff.GENERATED_DIR / r.template_id / "code.py").exists()


def test_self_repair_unsafe_then_badbuild_then_good(cleanup_generated):
    seq = [dict(UNSAFE_GEN), dict(BADBUILD_GEN), dict(GOOD_GEN)]
    with patch("api.freeform.codegen_provider.generate_template", side_effect=seq) as m:
        r = ff.generate_and_register("a block", [])
    if r.template_id:
        cleanup_generated.append(r.template_id)
    assert r.ok
    assert m.call_count == 3
    assert [a["stage"] for a in r.attempts] == ["validate", "run", "ok"]
    # retry feedback was actually passed on the 2nd/3rd calls
    assert m.call_args_list[1].kwargs.get("retry_feedback")


def test_oversize_part_fails_dfm_and_is_reported(cleanup_generated):
    with patch(
        "api.freeform.codegen_provider.generate_template",
        return_value=dict(OVERSIZE_GEN),
    ):
        r = ff.generate_and_register("a huge bar", [])
    if r.template_id:
        cleanup_generated.append(r.template_id)
    assert not r.ok
    assert all(a["stage"] in ("dfm",) for a in r.attempts)
    assert r.attempts[0]["dfm"]["within_size"] is False


def test_floating_pieces_fail_dfm_and_are_reported(cleanup_generated):
    """A design that leaves two disconnected bodies (floating pieces) is rejected
    even though each body is watertight, and the feedback names the problem."""
    with patch(
        "api.freeform.codegen_provider.generate_template",
        return_value=dict(FLOATING_GEN),
    ):
        r = ff.generate_and_register("two floating blocks", [])
    if r.template_id:
        cleanup_generated.append(r.template_id)
    assert not r.ok
    dfm = r.attempts[0]["dfm"]
    assert dfm["manifold"] is True  # each body is watertight…
    assert dfm["connected"] is False and dfm["body_count"] == 2  # …but 2 pieces
    assert "disconnected" in ff._dfm_feedback(dfm).lower()


def test_total_failure_logs_demand(tmp_path, monkeypatch):
    log = tmp_path / "demand_log.jsonl"
    monkeypatch.setattr(ff, "DEMAND_LOG", log)
    monkeypatch.setattr(ff, "DATA_DIR", tmp_path)
    with patch(
        "api.freeform.codegen_provider.generate_template", return_value=dict(UNSAFE_GEN)
    ):
        r = ff.generate_and_register("something impossible", [])
    assert not r.ok and r.error
    assert log.exists()
    assert "something impossible" in log.read_text()


def test_rehydrate_from_disk(cleanup_generated):
    import templates_lib.registry as reg

    with patch(
        "api.freeform.codegen_provider.generate_template", return_value=dict(GOOD_GEN)
    ):
        r = ff.generate_and_register("a block", [])
    cleanup_generated.append(r.template_id)
    gid = r.template_id
    reg._EPHEMERAL.clear()  # simulate a restart
    assert reg._EPHEMERAL.get(gid) is None
    spec = get_template(gid)  # should lazily reload from disk
    assert isinstance(spec, EphemeralTemplateSpec) and spec.code
