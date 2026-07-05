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
# A legitimate ASSEMBLY: two SEPARATE pieces (dict return) that fit together.
# Each is its own single manifold solid, so this PASSES (unlike FLOATING_GEN,
# which fuses two disjoint bodies into one broken part).
MULTI_GEN = {
    **GOOD_GEN,
    "cadquery_code": (
        "import cadquery as cq\n"
        "def build(params):\n"
        "    peg = cq.Workplane('XY').cylinder(float(params['w_mm']), 4)\n"
        "    base = cq.Workplane('XY').box(\n"
        "        float(params['d_mm']), float(params['d_mm']), float(params['h_mm'])\n"
        "    ).translate((0, 0, -float(params['w_mm']) / 2 - 6))\n"
        "    return {'peg': peg, 'base': base}\n"
    ),
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


def test_self_repair_unsafe_then_badbuild_then_good(cleanup_generated, monkeypatch):
    # Force single-candidate rounds (BEST_OF_N=1) so the self-repair loop is
    # sequential and deterministic: round 1 unsafe → round 2 bad-build → round 3
    # good, each round fed the previous round's failure as retry feedback.
    monkeypatch.setattr(ff, "BEST_OF_N", 1)
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


def test_multi_part_assembly_builds_separate_files(cleanup_generated):
    """A dict-return assembly passes DFM (each piece a single solid) and
    build_design emits one STEP/STL/3MF set per piece plus a merged assembly."""
    import shutil as _shutil

    from api.designs import EXPORTS_DIR, build_design

    with patch(
        "api.freeform.codegen_provider.generate_template", return_value=dict(MULTI_GEN)
    ):
        r = ff.generate_and_register("a peg that fits a base", [])
    if r.template_id:
        cleanup_generated.append(r.template_id)
    assert r.ok
    assert r.dfm["part_count"] == 2 and r.dfm["connected"] is True

    design_id, urls = build_design(r.template_id, {"w_mm": 22})
    try:
        assert {p["name"] for p in urls["parts"]} == {"peg", "base"}
        # Each part has its own three files + an ungated view mesh.
        for p in urls["parts"]:
            assert p["step"].endswith(".step") and p["stl"].endswith(".stl")
            assert p["threemf"].endswith(".3mf") and p["view_stl"]
        # A merged assembly for the composite + a combined view fallback.
        assert urls["stl"].endswith("assembly.stl")
        assert (EXPORTS_DIR / design_id / "peg.step").exists()
        assert (EXPORTS_DIR / design_id / "base.step").exists()
    finally:
        _shutil.rmtree(EXPORTS_DIR / design_id, ignore_errors=True)


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


# --- dimensional contract (Feature 3) ------------------------------------

# A build() that declares h_mm but IGNORES it (fixed height 20) — the classic
# "dead parameter" the dimensional contract must catch.
IGNORES_PARAM_GEN = {
    **GOOD_GEN,
    "cadquery_code": (
        "import cadquery as cq\n"
        "def build(params):\n"
        "    return cq.Workplane('XY').box(params['w_mm'], params['d_mm'], 20)\n"
    ),
}


def test_dimensional_contract_flags_dead_param():
    """Pure check: a length param whose perturbation moves neither bbox nor volume
    is flagged dead, named in the feedback, and drops the score below 1."""
    schema = ff.normalize_param_schema(GOOD_GEN["param_schema"])  # w_mm, d_mm, h_mm
    measurements = {
        "baseline": {"bbox": [40.0, 30.0, 20.0], "volume": 24000.0, "area": 5200.0},
        "probes": {
            "w_mm": {"built": True, "bbox": [50.0, 30.0, 20.0], "volume": 30000.0},
            "d_mm": {"built": True, "bbox": [40.0, 37.5, 20.0], "volume": 30000.0},
            "h_mm": {"built": True, "bbox": [40.0, 30.0, 20.0], "volume": 24000.0},
        },
    }
    ok, score, feedback, detail = ff.dimensional_contract_check(schema, measurements)
    assert ok is False
    assert detail["dead"] == ["h_mm"]
    assert "h_mm" in feedback
    assert 0.0 < score < 1.0


def test_dimensional_contract_passes_when_all_reflected():
    schema = ff.normalize_param_schema(GOOD_GEN["param_schema"])
    measurements = {
        "baseline": {"bbox": [40.0, 30.0, 20.0], "volume": 24000.0},
        "probes": {
            "w_mm": {"built": True, "bbox": [50.0, 30.0, 20.0], "volume": 30000.0},
            "d_mm": {"built": True, "bbox": [40.0, 37.5, 20.0], "volume": 30000.0},
            "h_mm": {"built": True, "bbox": [40.0, 30.0, 25.0], "volume": 30000.0},
        },
    }
    ok, score, feedback, detail = ff.dimensional_contract_check(schema, measurements)
    assert ok and score == 1.0 and feedback is None and detail["dead"] == []


def test_dimensional_contract_credits_internal_feature_via_volume():
    """A hole diameter changes volume but not the bounding box — it must still
    count as reflected (bbox-only checking would wrongly flag it dead)."""
    schema = ff.normalize_param_schema(
        [
            {
                "name": "hole_dia_mm",
                "type": "number",
                "default": 6,
                "minimum": 2,
                "maximum": 20,
            }
        ]
    )
    measurements = {
        "baseline": {"bbox": [40.0, 30.0, 20.0], "volume": 23434.0},
        "probes": {
            "hole_dia_mm": {
                "built": True,
                "bbox": [40.0, 30.0, 20.0],
                "volume": 22000.0,
            },
        },
    }
    ok, score, feedback, detail = ff.dimensional_contract_check(schema, measurements)
    assert ok and detail["dead"] == []


def test_ignored_param_rejected_by_generation_loop(cleanup_generated):
    """End-to-end: a design that ignores h_mm never ships (fails the contract every
    round) and lands in the demand log with a param-named reason."""
    with patch(
        "api.freeform.codegen_provider.generate_template",
        return_value=dict(IGNORES_PARAM_GEN),
    ):
        r = ff.generate_and_register("a block that ignores height", [])
    if r.template_id:
        cleanup_generated.append(r.template_id)
    assert not r.ok
    assert all(a["stage"] == "dimcontract" for a in r.attempts)


# --- best-of-N (Feature 2) -----------------------------------------------


def test_best_of_n_evaluates_three_and_records_candidates(cleanup_generated):
    """The first round fans out to BEST_OF_N candidates; all are recorded in
    provenance with scores, exactly one is the winner, and losers keep their
    code + scores for the review page."""
    with patch(
        "api.freeform.codegen_provider.generate_template", return_value=dict(GOOD_GEN)
    ) as m:
        r = ff.generate_and_register("a block", [])
    cleanup_generated.append(r.template_id)
    assert r.ok
    assert m.call_count == ff.BEST_OF_N == 3
    assert len(r.candidates) == 3
    assert sum(1 for c in r.candidates if c["winner"]) == 1
    for c in r.candidates:
        assert c["code"] and c["param_schema"] and "score" in c
    # A shippable candidate scores in [0.5, 1.0].
    assert r.score is not None and r.score >= 0.5


# --- visual critique loop (Feature 1) ------------------------------------


def test_critique_below_threshold_regenerates_with_fixes(
    cleanup_generated, monkeypatch
):
    """With critique enabled, a below-0.7 visual match triggers a regeneration whose
    prompt carries the targeted fixes; a subsequent passing critique wins."""
    monkeypatch.setenv("VULCAN_CRITIQUE", "on")
    monkeypatch.setattr(ff, "BEST_OF_N", 1)  # deterministic single-candidate rounds
    critiques = [
        {
            "matches_request": 0.4,
            "defects": ["too plain"],
            "targeted_fixes": ["add a chamfer"],
        },
        {"matches_request": 0.92, "defects": [], "targeted_fixes": []},
    ]
    with patch(
        "api.freeform.codegen_provider.generate_template", return_value=dict(GOOD_GEN)
    ) as m, patch(
        "api.freeform.vision_provider.critique_design", side_effect=critiques
    ) as cm:
        r = ff.generate_and_register("a nice block", [])
    cleanup_generated.append(r.template_id)
    assert r.ok
    assert cm.call_count == 2 and m.call_count == 2
    assert r.critique["matches_request"] == 0.92
    # The regeneration prompt included the fix from the first critique.
    assert "add a chamfer" in (m.call_args_list[1].kwargs.get("retry_feedback") or "")
    # All critiques are stored in provenance.
    from api.freeform import GENERATED_DIR
    import json as _json

    prov = _json.loads((GENERATED_DIR / r.template_id / "provenance.json").read_text())
    assert len(prov["critiques"]) == 2


def test_critique_disabled_skips_and_still_succeeds(cleanup_generated):
    """Default (critique off): no vision call, critique is None, design still ships."""
    with patch(
        "api.freeform.codegen_provider.generate_template", return_value=dict(GOOD_GEN)
    ), patch("api.freeform.vision_provider.critique_design") as cm:
        r = ff.generate_and_register("a block", [])
    cleanup_generated.append(r.template_id)
    assert r.ok and r.critique is None
    cm.assert_not_called()
