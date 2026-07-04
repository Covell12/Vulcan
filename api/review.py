"""Founder review queue + the download gate for freeform designs (Track B).

Non-negotiable: every freeform design lands in `pending_review`, and its
downloadable CAD files (STEP/STL/3MF) are BLOCKED until the founder approves it.
The preview PNG is always viewable (the founder and the user both need to see
the render). Track A designs have no record and are never gated.

Endpoints:
  GET  /review                 — list design records (default: pending only)
  GET  /review/{design_id}     — one record (request, code, params, dfm, verdict)
  POST /review/{design_id}     — approve/reject with a note
  GET  /exports/{id}/{file}    — gated download (registered BEFORE the static
                                 /exports mount in api/main, so it takes
                                 precedence for these two-segment file paths)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api import design_store
from api.design_store import STATUS_APPROVED, STATUS_REJECTED

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
EXPORTS_DIR = BASE_DIR / "exports"

# The CAD deliverables gated behind approval. The preview PNG is intentionally
# NOT here — it must stay viewable while pending. Compared case-folded (a
# case-insensitive filesystem would otherwise serve `PART.STL` ungated).
_GATED_FILES = {"part.step", "part.stl", "part.3mf"}

# Optional founder secret for the approve/reject endpoint (security review: the
# review gate is worthless if anyone can self-approve). When VULCAN_REVIEW_TOKEN
# is set, POST /review/{id} requires a matching X-Review-Token header; when it's
# unset (local dev), the endpoint is open — this MUST be set before the API is
# exposed beyond localhost.
_REVIEW_TOKEN_ENV = "VULCAN_REVIEW_TOKEN"


def _check_review_auth(token: str | None) -> None:
    expected = os.getenv(_REVIEW_TOKEN_ENV, "").strip()
    if expected and token != expected:
        raise HTTPException(
            status_code=403,
            detail="founder review token required (X-Review-Token) to record a verdict.",
        )


def _founder_authorized(token: str | None) -> bool:
    """Whether a request is the FOUNDER (who may download a design's files even
    while it's pending — the review gate exists to stop the CUSTOMER getting
    files early, not the reviewer). A missing token (the default customer path)
    is never authorized. When no token is configured (local dev) any presented
    token works, so the founder dashboard can download without setup."""
    if token is None:
        return False
    expected = os.getenv(_REVIEW_TOKEN_ENV, "").strip()
    return expected == "" or token == expected


class ReviewVerdict(BaseModel):
    verdict: str  # "approve" | "reject"
    note: str | None = None


@router.get("/review")
def list_review(status: str = "pending") -> list[dict[str, Any]]:
    """List design records. `status=pending` (default) shows only what needs a
    verdict; `status=all` shows everything."""
    records = design_store.list_records()
    if status == "all":
        return records
    return [r for r in records if r.get("status") == design_store.STATUS_PENDING]


@router.get("/review/{design_id}")
def get_review(design_id: str) -> dict[str, Any]:
    record = design_store.load_record(design_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No design record '{design_id}'.")
    return record


@router.post("/review/{design_id}")
def submit_verdict(
    design_id: str,
    body: ReviewVerdict,
    x_review_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_review_auth(x_review_token)
    record = design_store.load_record(design_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No design record '{design_id}'.")

    verdict = body.verdict.strip().lower()
    if verdict not in ("approve", "reject"):
        raise HTTPException(
            status_code=422, detail="verdict must be 'approve' or 'reject'."
        )

    new_status = STATUS_APPROVED if verdict == "approve" else STATUS_REJECTED
    updated = design_store.update_record(
        design_id,
        status=new_status,
        review_note=body.note,
        reviewed_at=datetime.now(timezone.utc).isoformat(),
    )
    return updated  # type: ignore[return-value]


@router.get("/exports/{design_id}/{filename}")
def download_export(
    design_id: str,
    filename: str,
    x_review_token: str | None = Header(default=None),
) -> FileResponse:
    """Serve a generated file, enforcing the freeform review gate. The ONLY route
    that serves /exports (there is no StaticFiles mount over the same dir), so no
    non-canonical spelling can slip a gated file out under an ungated path.
    Track A designs (no record) pass straight through. The FOUNDER dashboard may
    download a pending design's files by presenting the review token (the gate is
    to stop the customer, not the reviewer)."""
    # Reject anything that isn't a plain <id>/<file.ext> — no separators, no
    # dot-segments, no absolute/traversal shapes. This is the whole surface.
    for part in (design_id, filename):
        if (
            not part
            or "/" in part
            or "\\" in part
            or ".." in part
            or part.startswith(".")
        ):
            raise HTTPException(status_code=404, detail="Not found.")

    record = design_store.load_record(design_id)
    # Case-fold the gate test: a case-insensitive filesystem would otherwise
    # serve `PART.STL` (not in the set) which resolves to the gated `part.stl`.
    is_cad = filename.lower() in _GATED_FILES
    if (
        record is not None
        and record.get("is_freeform")
        and record.get("status") != STATUS_APPROVED
        and is_cad
        and not _founder_authorized(x_review_token)
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "This custom design is pending a human review and can't be "
                "downloaded yet. It ships once the founder approves it."
            ),
        )

    # Resolve and confirm the path stays inside EXPORTS_DIR (defense in depth).
    file_path = (EXPORTS_DIR / design_id / filename).resolve()
    exports_root = EXPORTS_DIR.resolve()
    if exports_root not in file_path.parents or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(str(file_path))
