"""Vulcan Core API. `uvicorn api.main:app --reload` serves the API + test UI."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api import (
    freeform,
)  # noqa: F401  (import side effect: sets the ephemeral-template loader)
from api import vision_provider
from api.depth_provider import check_provider_configured as check_depth_configured
from api.designs import router as designs_router
from api.intents import router as intents_router
from api.review import router as review_router
from api.templates import router as templates_router
from api.vision_provider import check_provider_configured as check_vision_configured

# Log to uvicorn's own logger so these lines show up in the server console.
_log = logging.getLogger("uvicorn.error")

BASE_DIR = Path(__file__).resolve().parent.parent
EXPORTS_DIR = BASE_DIR / "exports"
WEB_DIR = BASE_DIR / "web"

EXPORTS_DIR.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # This whole hook only runs on a real ASGI startup (uvicorn, or
    # `with TestClient(app) as c`), never a plain `TestClient(app)` — so it never
    # gets in the way of tests that mock the providers and don't need real keys.

    # Say which vision provider is actually in effect, and shout if a shell env
    # var is silently shadowing a different VISION_PROVIDER in .env (load_dotenv
    # does NOT override an OS env var — the classic "I set .env but it's ignored"
    # trap). This runs BEFORE the credential check on purpose: if the shell forces
    # a provider whose key is missing, the check below raises naming that provider,
    # so the founder needs this explanation first.
    _log.info("Vision provider: %s", vision_provider.get_provider_name())
    shadow = vision_provider.env_shadowing("VISION_PROVIDER")
    if shadow:
        os_val, file_val = shadow
        _log.warning(
            "VISION_PROVIDER=%r from your shell environment is OVERRIDING "
            "VISION_PROVIDER=%r in .env — a shell variable beats .env, so your "
            ".env edit has no effect. Run `unset VISION_PROVIDER` (and remove any "
            "`export VISION_PROVIDER=...` from your shell profile) to use .env.",
            os_val,
            file_val,
        )

    # Fails fast, with a clear message, if a selected provider's credentials
    # aren't configured — better than a confusing error the first time someone
    # hits POST /intents.
    check_vision_configured()
    check_depth_configured()
    yield


app = FastAPI(title="Vulcan Core API", lifespan=lifespan)

# CORS. The web/ frontend is a standalone static client that may be hosted on a
# different origin than this API (see web/README.md — it talks to the API purely
# over HTTP via web/api.js). This lets a browser on the configured origins call
# the API. VULCAN_CORS_ORIGINS is a comma-separated allowlist; the default "*"
# is fine for local dev and same-origin deploys. No cookies are used (the founder
# review token is a request header), so credentials stay OFF — which is exactly
# what makes a "*" allowlist safe here.
_cors_origins = [
    o.strip() for o in os.getenv("VULCAN_CORS_ORIGINS", "*").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


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
