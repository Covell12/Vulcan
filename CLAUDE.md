# Vulcan — Project Context for Claude Code

Vulcan turns a photo + a sentence into a custom physical part, delivered. The user
photographs where the part goes, roughly circles/sketches it, answers measurement
questions, approves a render, and pays. Phase 0: the founder prints everything on his
own FDM printers. This repo is the Vulcan Core API + a minimal test web UI.

Full spec: `docs/vulcan-product-spec.pdf` (v0.2). Read it before large design decisions.

## Architecture (v0)

- `api/` — FastAPI app (Python 3.11, Pydantic v2). All product logic lives behind the API.
- `templates_lib/` — parametric part templates in **CadQuery** (code-CAD). Each template =
  one Python module + parameter schema + DFM rules + pytest file. Templates are the
  DEFAULT design path ("Track A"). Freeform generation comes much later.
- `web/` — v0 test UI: plain static HTML/JS served BY FastAPI (no Next.js, no build step yet).
  Its only job: exercise the API as it grows (upload photo → see questions → preview → params → download files).
- `schemas/` — canonical JSON Schemas (IntentSpec etc.). API responses must validate against these.
- `docs/` — spec PDF, roadmap.

## Pipeline (target; build incrementally)

intent (photos+annotation+text) → dimension/scale resolution → design synthesis (template)
→ DFM validation → preview render → quote → order → fulfillment queue → fit-outcome record.

## Non-negotiable dimension rules

1. Every dimension carries `source` (`user_measured` | `depth_inferred` | `assumed`) and `confidence` (0–1).
2. Fit-critical dimensions (marked `critical: true` in template schemas) may ONLY commit
   from `user_measured`. Depth models PROPOSE; the user CONFIRMS.
3. Cross-check: if a user-typed value differs from the depth prior by >20%, re-ask
   (likely mm/cm/inch mistake). Never silently override the user.
4. All internal units are millimeters. Accept inches in UI, convert at the boundary.

## Engineering conventions

- Python 3.11, FastAPI, Pydantic v2, pytest. Type hints everywhere. Format with black.
- Deterministic core: template geometry generation must be pure functions of parameters.
- Every template needs tests: manifold mesh, min wall ≥ 2.4mm (PETG default), parameter
  validation ranges enforced, STEP + 3MF + STL export succeeds.
- Exports via CadQuery: STEP (manufacturing), 3MF/STL (printing), PNG render (preview).
- No secrets in code. Use `.env` (python-dotenv); `ANTHROPIC_API_KEY` for intent parsing (later milestone).
- Prefer boring, debuggable solutions. This is a solo-founder production system, not a demo.

## EXPLANATIONS.md rule (required)

After creating or materially changing any file, add/update its entry in `EXPLANATIONS.md`:
path, one plain-English paragraph a non-expert can follow (what it does, why it exists,
what talks to it). Keep entries current — stale explanations are bugs.

## Run commands

- API: `uvicorn api.main:app --reload` (serves web UI at http://localhost:8000)
- Tests: `pytest -q`
- Never commit `.env`, exports/, or __pycache__.
