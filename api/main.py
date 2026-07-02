"""Vulcan Core API. `uvicorn api.main:app --reload` serves the API + test UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.designs import router as designs_router

BASE_DIR = Path(__file__).resolve().parent.parent
EXPORTS_DIR = BASE_DIR / "exports"
WEB_DIR = BASE_DIR / "web"

EXPORTS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Vulcan Core API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(designs_router)

# Generated part files (STEP/3MF/STL/preview PNG), one subdirectory per design_id.
app.mount("/exports", StaticFiles(directory=EXPORTS_DIR), name="exports")

# Static test UI. Mounted last/at "/" so it doesn't shadow the API routes above.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
