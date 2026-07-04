"""Design records for freeform (Track B) designs — the founder-review corpus.

One JSON file per design under data/designs/<design_id>.json (same boring
file-per-record persistence as intents). A record captures the whole freeform
provenance: the request, the generated code, the resolved params, the DFM/
manifold results, the produced files, and the founder's verdict + note.

Only FREEFORM designs get a record; Track A (registry-template) designs don't,
so the download gate (api/review) leaves them ungated. This record set is also
the templatization-mining data: request → code → verdict → note.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DESIGNS_DIR = DATA_DIR / "designs"

# Verdict lifecycle for a freeform design record.
STATUS_PENDING = "pending_review"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


def _record_path(design_id: str) -> Path:
    return DESIGNS_DIR / f"{design_id}.json"


def save_record(record: dict[str, Any]) -> None:
    DESIGNS_DIR.mkdir(parents=True, exist_ok=True)
    with open(_record_path(record["design_id"]), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)


def load_record(design_id: str) -> dict[str, Any] | None:
    path = _record_path(design_id)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def update_record(design_id: str, **changes: Any) -> dict[str, Any] | None:
    record = load_record(design_id)
    if record is None:
        return None
    record.update(changes)
    save_record(record)
    return record


def list_records() -> list[dict[str, Any]]:
    """All records, newest first (by created_at)."""
    if not DESIGNS_DIR.exists():
        return []
    records = []
    for path in DESIGNS_DIR.glob("*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                records.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            continue
    records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return records
