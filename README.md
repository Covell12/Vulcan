# Vulcan

**Describe it. Hold it.** A photo and a sentence in; a custom physical part out.

This repo: the Vulcan Core API (FastAPI), the parametric template library (CadQuery),
and a minimal web test UI served by the API.

## Setup

```bash
python3.11 -m venv .venv         # Python 3.11+ (3.13 confirmed working)
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # add keys as milestones require them
```

## Run

```bash
uvicorn api.main:app --reload    # API + test UI at http://localhost:8000
pytest -q                        # tests
```

The server fails fast on startup if a selected provider's credentials aren't set in
`.env` (the vision provider always; the depth provider only when `DEPTH_PROVIDER=replicate`).

## Vision provider (intent parser)

Set `VISION_PROVIDER=openai` or `VISION_PROVIDER=anthropic` in `.env` and fill in that
provider's key (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`) — that's the only edit needed
to switch providers; restart the server to pick it up. Override the model with
`VISION_MODEL`. See `api/vision_provider.py` for the one place this logic lives.

## Depth provider (metric-scale prior, optional)

`DEPTH_PROVIDER=none` (the default) runs everything with no depth model — questions
still get asked and every fit-critical dimension still requires a real user
measurement. `DEPTH_PROVIDER=replicate` adds metric-size *suggestions* from the photo
(prefilling "looks like ~210mm — measure to confirm") and enables the unit-mistake
cross-check: a typed value that disagrees with the depth prior by more than 20% is
re-asked, never silently committed. Set `REPLICATE_API_TOKEN` and, because no public
Replicate model currently returns raw metric depth, point `DEPTH_MODEL` at a cog that
returns per-pixel metric depth + focal length. See `api/depth_provider.py` — the one
place this logic lives — for the exact output contract.

## Where things are

See `EXPLANATIONS.md` for a plain-English map of every file.
See `docs/ROADMAP.md` for the build order. Spec: `docs/vulcan-product-spec.pdf`.
