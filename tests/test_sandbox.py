"""Security + behavior tests for api/sandbox.py — the Track B containment
boundary for untrusted LLM-generated code.

The malicious cases (os import, file open, network) must be rejected at the
`verify` stage — i.e. WITHOUT ever spawning the subprocess — and must leave no
side effects. The infinite loop must be killed by the wall-clock timeout. A
legitimate build must export STEP/STL/3MF.
"""

from __future__ import annotations

from pathlib import Path

from api.sandbox import run_generated_build

GOOD = """
import cadquery as cq

def build(params):
    return cq.Workplane("XY").box(params["w"], params["d"], params["h"])
"""


def test_valid_build_exports_all_three_formats(tmp_path: Path):
    r = run_generated_build(GOOD, {"w": 40, "d": 30, "h": 20}, tmp_path / "out")
    assert r.ok and r.stage == "ok", r.error
    assert set(r.files) == {"step", "stl", "threemf"}
    for p in r.files.values():
        assert p.exists() and p.stat().st_size > 0


def test_os_import_rejected_at_verify_and_never_runs(tmp_path: Path):
    marker = tmp_path / "PWNED"
    code = (
        "import os\n"
        "def build(params):\n"
        f"    os.system('touch {marker}')\n"
        "    return None\n"
    )
    r = run_generated_build(code, {}, tmp_path / "out")
    assert not r.ok and r.stage == "verify"
    assert not marker.exists(), "verify-stage rejection must not execute the code"
    assert not (tmp_path / "out").exists(), "no output dir for rejected code"


def test_file_open_rejected_at_verify(tmp_path: Path):
    code = "def build(params):\n    return open('/etc/passwd').read()\n"
    r = run_generated_build(code, {}, tmp_path / "out")
    assert not r.ok and r.stage == "verify"


def test_network_import_rejected_at_verify(tmp_path: Path):
    code = "import socket\ndef build(params):\n    return socket.socket()\n"
    r = run_generated_build(code, {}, tmp_path / "out")
    assert not r.ok and r.stage == "verify"


def test_dunder_smuggling_rejected_at_verify(tmp_path: Path):
    code = (
        "def build(params):\n" "    return ().__class__.__bases__[0].__subclasses__()\n"
    )
    r = run_generated_build(code, {}, tmp_path / "out")
    assert not r.ok and r.stage == "verify"


def test_infinite_loop_hits_timeout(tmp_path: Path):
    code = "import cadquery as cq\ndef build(params):\n    while True:\n        pass\n"
    r = run_generated_build(code, {}, tmp_path / "out", timeout_s=3)
    assert not r.ok and r.stage == "timeout"


def test_submodule_attribute_escape_rejected_at_verify(tmp_path: Path):
    # The red-team escape: reach the real `os` via a submodule attribute chain of
    # an allowed package. Must be rejected at verify — never executed.
    marker = tmp_path / "ESCAPED"
    code = (
        "import cadquery.occ_impl.exporters as ex\n"
        "def build(params):\n"
        f"    ex.os.system('touch {marker}')\n"
        "    return None\n"
    )
    r = run_generated_build(code, {}, tmp_path / "out")
    assert not r.ok and r.stage == "verify"
    assert not marker.exists()


def test_build_runtime_error_reported_for_self_repair(tmp_path: Path):
    # A degenerate box raises inside CadQuery; comes back as a run-stage error
    # (with a message) rather than a crash — this is what feeds self-repair.
    code = "import cadquery as cq\ndef build(params):\n    return cq.Workplane('XY').box(0, 0, 0)\n"
    r = run_generated_build(code, {}, tmp_path / "out")
    assert not r.ok and r.stage == "run"
    assert r.error
