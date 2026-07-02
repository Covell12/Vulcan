"""Vulcan Core API. `uvicorn api.main:app --reload` serves the API + test UI."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.designs import router as designs_router
from api.intents import router as intents_router
from api.templates import router as templates_router
from api.vision_provider import check_provider_configured

BASE_DIR = Path(__file__).resolve().parent.parent
EXPORTS_DIR = BASE_DIR / "exports"
WEB_DIR = BASE_DIR / "web"

EXPORTS_DIR.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fails fast, with a clear message, if VISION_PROVIDER is set to a
    # provider whose API key isn't configured — better than a confusing
    # error the first time someone hits POST /intents. Only runs when the
    # ASGI server actually starts (uvicorn, or `with TestClient(app) as c`),
    # not for a plain `TestClient(app)` — so it never gets in the way of
    # tests that mock the provider and don't need a real key.
    check_provider_configured()
    yield


app = FastAPI(title="Vulcan Core API", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(designs_router)
app.include_router(templates_router)
app.include_router(intents_router)

# Generated part files (STEP/3MF/STL/preview PNG), one subdirectory per design_id.
app.mount("/exports", StaticFiles(directory=EXPORTS_DIR), name="exports")

# Static test UI. Mounted last/at "/" so it doesn't shadow the API routes above.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
