"""Tests for api/jobs.py — the in-process async job registry backing the freeform
generation endpoint. No network: targets are plain callables."""

from __future__ import annotations

import threading
import time

from api import jobs


def test_sync_execution_runs_inline_and_marks_ready():
    seen_stages = []
    out = jobs.start(
        "intent-1",
        lambda progress: (progress("critiquing"), {"ok": True, "value": 7})[1],
        sync=True,
    )
    job = jobs.get_job(out)
    assert job["status"] == "ready" and job["stage"] == "ready"
    assert job["result"]["value"] == 7
    assert job["intent_id"] == "intent-1"


def test_ok_false_payload_marks_failed():
    jid = jobs.start("i2", lambda progress: {"ok": False, "error": "nope"}, sync=True)
    job = jobs.get_job(jid)
    assert job["status"] == "failed" and job["error"] == "nope"


def test_raised_exception_marks_failed():
    def boom(progress):
        raise RuntimeError("kaboom")

    jid = jobs.start("i3", boom, sync=True)
    job = jobs.get_job(jid)
    assert job["status"] == "failed" and "kaboom" in job["error"]


def test_async_execution_transitions_to_ready():
    gate = threading.Event()

    def target(progress):
        progress("generating")
        gate.wait(timeout=5)
        return {"ok": True}

    jid = jobs.start("i4", target, sync=False)
    # Before releasing the gate the job is still running.
    assert jobs.get_job(jid)["status"] in ("generating", "queued")
    gate.set()
    for _ in range(50):
        if jobs.get_job(jid)["status"] == "ready":
            break
        time.sleep(0.05)
    assert jobs.get_job(jid)["status"] == "ready"


def test_unknown_job_is_none():
    assert jobs.get_job("does-not-exist") is None
