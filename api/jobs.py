"""In-process async jobs for slow freeform generation (Track B, M10a).

Generation now runs BEST-OF-N candidates plus a visual critique per candidate,
which can take a while — so the HTTP request that starts it returns immediately
with a job_id + status_url and the client polls. Jobs live in memory (a dict
guarded by a lock) and run on a daemon background thread; that's right for the
single-founder Phase-0 deployment (a multi-worker deploy would swap this for a
real queue — the API surface wouldn't change).

Stage lifecycle: queued → generating → critiquing → ready | failed. The
generation loop reports 'generating' then 'critiquing' via the `progress`
callback it's handed.

Tests set VULCAN_JOBS_SYNC=1 so start() runs the target INLINE (same code path,
no thread) — deterministic, and any provider mock the test installed stays active
for the whole run instead of racing a background thread.
"""

from __future__ import annotations

import os
import threading
import uuid
from typing import Any, Callable

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}

# The most recent jobs are kept; older ones are trimmed so the dict can't grow
# without bound in a long-lived server.
_MAX_JOBS = 200


def create_job(intent_id: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        _JOBS[job_id] = {
            "job_id": job_id,
            "intent_id": intent_id,
            "status": "queued",
            "stage": "queued",
            "error": None,
            "result": None,
        }
        if len(_JOBS) > _MAX_JOBS:
            for stale in list(_JOBS)[: len(_JOBS) - _MAX_JOBS]:
                _JOBS.pop(stale, None)
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def _set(job_id: str, **changes: Any) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            job.update(changes)


def _run_sync_requested() -> bool:
    return os.getenv("VULCAN_JOBS_SYNC", "").strip().lower() in (
        "1",
        "on",
        "true",
        "yes",
    )


def _execute(
    job_id: str, target: Callable[[Callable[[str], None]], dict[str, Any]]
) -> None:
    def progress(stage: str) -> None:
        _set(job_id, stage=stage, status=stage)

    _set(job_id, status="generating", stage="generating")
    try:
        payload = target(progress) or {}
        ok = bool(payload.get("ok"))
        _set(
            job_id,
            status="ready" if ok else "failed",
            stage="ready" if ok else "failed",
            result=payload,
            error=payload.get("error"),
        )
    except Exception as e:  # noqa: BLE001 — surface any failure to the poller
        _set(job_id, status="failed", stage="failed", error=f"{type(e).__name__}: {e}")


def start(
    intent_id: str,
    target: Callable[[Callable[[str], None]], dict[str, Any]],
    *,
    sync: bool | None = None,
) -> str:
    """Create a job and run `target(progress)` — on a background thread normally,
    or inline when sync (VULCAN_JOBS_SYNC) is set. `target` returns a payload dict
    with at least {"ok": bool}; ok=False (or a raised exception) marks the job
    failed. Returns the job_id immediately."""
    job_id = create_job(intent_id)
    run_sync = sync if sync is not None else _run_sync_requested()
    if run_sync:
        _execute(job_id, target)
    else:
        threading.Thread(target=_execute, args=(job_id, target), daemon=True).start()
    return job_id
