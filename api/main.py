"""Vulcan Core API. `uvicorn api.main:app --reload` serves the API + test UI."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api import (
    freeform,
)  # noqa: F401  (import side effect: sets the ephemeral-template loader)
from api.depth_provider import check_provider_configured as check_depth_configured
from api.designs import router as designs_router
from api.intents import router as intents_router
from api.review import router as review_router
from api.templates import router as templates_router
from api.vision_provider import check_provider_configured as check_vision_configured

BASE_DIR = Path(__file__).resolve().parent.parent
EXPORTS_DIR = BASE_DIR / "exports"
WEB_DIR = BASE_DIR / "web"

EXPORTS_DIR.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fails fast, with a clear message, if a selected provider's credentials
    # aren't configured — better than a confusing error the first time someone
    # hits POST /intents. The depth check is a no-op for DEPTH_PROVIDER=none
    # (the default), so depth stays fully optional. Only runs on a real ASGI
    # startup (uvicorn, or `with TestClient(app) as c`), not a plain
    # `TestClient(app)` — so it never gets in the way of tests that mock the
    # providers and don't need real keys.
    check_vision_configured()
    check_depth_configured()
    yield


app = FastAPI(title="Vulcan Core API", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(designs_router)
app.include_router(templates_router)
app.include_router(intents_router)
# review_router owns GET /exports/{id}/{file}. We deliberately do NOT ALSO mount
# StaticFiles at /exports: a permissive mount over the same directory let gated
# CAD files be fetched under non-canonical spellings (casing, trailing/double
# slash, %2e) that missed the gate route (security review). ALL export serving
# now goes through the single gated handler; a non-canonical path just 404s.
app.include_router(review_router)

# Static test UI. Mounted last/at "/" so it doesn't shadow the API routes above.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
