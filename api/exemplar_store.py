"""Approved-design exemplar memory (Track B generation quality).

Every APPROVED freeform design is stored here as (request_text, param_schema,
code) under data/exemplars/<id>.json. At generation time we retrieve the top-k by
embedding similarity to the new request and feed them to the codegen model as
EXTRA few-shot examples — so the system learns from designs the founder has
already blessed, instead of only the two hand-written static exemplars.

Embeddings are computed at RETRIEVE time (not stored) via api/embedding_provider,
so the query and every candidate share one provider/dimension even if the
configured provider changed since a design was approved. If the embedding
provider is unavailable (e.g. OpenAI offline), retrieval silently falls back to
the deterministic local embedding, so it always returns a sensible ordering.

Falls back to the codegen module's two static exemplars when the store is empty
(the caller checks for an empty list).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api import embedding_provider

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
EXEMPLARS_DIR = DATA_DIR / "exemplars"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_exemplar(
    request_text: str,
    param_schema: list[dict[str, Any]],
    code: str,
    *,
    design_id: str | None = None,
) -> str:
    """Store one approved design as a reusable exemplar. Idempotent per design_id:
    re-approving the same design overwrites rather than duplicates. Returns the
    exemplar id."""
    EXEMPLARS_DIR.mkdir(parents=True, exist_ok=True)
    exemplar_id = None
    if design_id:
        # One exemplar per design_id — find an existing file to overwrite.
        for rec in _load_all():
            if rec.get("design_id") == design_id:
                exemplar_id = rec["id"]
                break
    exemplar_id = exemplar_id or uuid.uuid4().hex[:12]
    record = {
        "id": exemplar_id,
        "design_id": design_id,
        "request_text": request_text or "",
        "param_schema": param_schema or [],
        "code": code or "",
        "created_at": _now(),
    }
    with open(EXEMPLARS_DIR / f"{exemplar_id}.json", "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    return exemplar_id


def _load_all() -> list[dict[str, Any]]:
    if not EXEMPLARS_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in EXEMPLARS_DIR.glob("*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed via the configured provider; on any provider failure fall back to the
    deterministic local embedding so retrieval always works offline."""
    try:
        return embedding_provider.embed(texts)
    except embedding_provider.EmbeddingProviderError:
        return [embedding_provider.local_embedding(t) for t in texts]


def retrieve(request_text: str, k: int = 2) -> list[dict[str, Any]]:
    """Return the top-k stored exemplars most similar to `request_text`, each with
    a `similarity` score, most-similar first. Empty list when the store is empty
    (caller then uses the static exemplars). Ties broken by recency."""
    records = _load_all()
    if not records:
        return []
    texts = [request_text or ""] + [r.get("request_text", "") for r in records]
    vecs = _embed_texts(texts)
    query_vec, doc_vecs = vecs[0], vecs[1:]
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for rec, vec in zip(records, doc_vecs):
        scored.append((_cosine(query_vec, vec), rec.get("created_at", ""), rec))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [{**rec, "similarity": round(sim, 4)} for sim, _, rec in scored[:k]]


def count() -> int:
    return len(_load_all())
