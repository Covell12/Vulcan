"""The ONE seam for text embeddings in Vulcan (Track B exemplar memory).

Same shape as api/vision_provider.py / api/codegen_provider.py: callers use
`embed()` / `embed_one()` and never import an SDK or branch on a provider name.
EMBEDDING_PROVIDER selects the backend (openai|anthropic|local, default openai);
EMBEDDING_MODEL overrides the model.

  - openai: the real embeddings endpoint (text-embedding-3-small by default).
  - anthropic: Anthropic has NO native embeddings endpoint, so this transparently
    uses the deterministic LOCAL embedding below — the seam still works and the
    exemplar store keeps functioning, just with a bag-of-words vector.
  - local: a deterministic, dependency-free hashed bag-of-words embedding.
    Offline, reproducible (used by tests), and good enough to rank request text
    by topical overlap.

Only api/vision_provider.py, api/codegen_provider.py, and this module may import
a provider SDK (enforced by the isolation test); this seam is on that allowlist
because the OpenAI embeddings call needs the `openai` SDK.

`override=True`: .env is authoritative over a stale shell var — same rationale as
the other provider seams.
"""

from __future__ import annotations

import hashlib
import math
import os
import re

from dotenv import load_dotenv

load_dotenv(override=True)

_DEFAULT_MODELS = {"openai": "text-embedding-3-small"}
_LOCAL_DIM = 256
_TOKEN_RE = re.compile(r"[a-z0-9]+")


class EmbeddingProviderError(RuntimeError):
    """The only exception type that may escape this module (openai path). The
    local path never raises — it's pure arithmetic."""


def get_provider_name() -> str:
    return os.getenv("EMBEDDING_PROVIDER", "openai").strip().lower()


def get_model_name(provider: str | None = None) -> str:
    provider = provider or get_provider_name()
    return os.getenv("EMBEDDING_MODEL") or _DEFAULT_MODELS.get(
        provider, "text-embedding-3-small"
    )


def embed(texts: list[str]) -> list[list[float]]:
    """Embed each text. openai → the real endpoint (may raise
    EmbeddingProviderError); anthropic/local/unknown → the deterministic local
    embedding (never raises). All vectors from one call share a dimension."""
    provider = get_provider_name()
    if provider == "openai":
        return _embed_openai(texts, get_model_name("openai"))
    return [local_embedding(t) for t in texts]


def embed_one(text: str) -> list[float]:
    return embed([text])[0]


def local_embedding(text: str) -> list[float]:
    """Deterministic, offline, dependency-free embedding: signed feature-hashing
    of the text's tokens into a fixed-dim L2-normalized vector. Cosine similarity
    then reflects shared vocabulary, so 'a bridge to span two shelves' ranks a
    stored bridge design above a knob. Used directly as the offline fallback."""
    vec = [0.0] * _LOCAL_DIM
    for tok in _TOKEN_RE.findall((text or "").lower()):
        h = int(hashlib.sha1(tok.encode("utf-8")).hexdigest(), 16)
        idx = h % _LOCAL_DIM
        sign = 1.0 if (h >> 8) & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _embed_openai(texts: list[str], model: str) -> list[list[float]]:
    try:
        import openai

        client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        resp = client.embeddings.create(model=model, input=texts)
        return [list(d.embedding) for d in resp.data]
    except Exception as e:  # noqa: BLE001 — any failure becomes our one error type
        raise EmbeddingProviderError(
            f"OpenAI embeddings failed ({type(e).__name__}: {e})"
        ) from e
