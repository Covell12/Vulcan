"""Shared pytest fixtures / test-environment defaults.

The freeform generation loop (Track B, M10a) calls a vision provider to critique
each candidate's renders. That's real network work, so it's gated by
VULCAN_CRITIQUE and defaulted OFF here — the whole suite runs offline and
deterministic, and the tests that exercise the critique loop opt IN explicitly
(setting VULCAN_CRITIQUE=on and patching vision_provider.critique_design).

Embeddings likewise default to the deterministic LOCAL provider so exemplar
retrieval is reproducible and needs no network/key.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _offline_generation_defaults(monkeypatch):
    monkeypatch.setenv("VULCAN_CRITIQUE", "off")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    # Run freeform jobs INLINE (no background thread) so a provider mock installed
    # by a test stays active for the whole run and polling is deterministic.
    monkeypatch.setenv("VULCAN_JOBS_SYNC", "1")
