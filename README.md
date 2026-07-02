# Vulcan

**Describe it. Hold it.** A photo and a sentence in; a custom physical part out.

This repo: the Vulcan Core API (FastAPI), the parametric template library (CadQuery),
and a minimal web test UI served by the API.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # add keys as milestones require them
```

## Run

```bash
uvicorn api.main:app --reload    # API + test UI at http://localhost:8000
pytest -q                        # tests
```

## Where things are

See `EXPLANATIONS.md` for a plain-English map of every file.
See `docs/ROADMAP.md` for the build order. Spec: `docs/vulcan-product-spec.pdf`.
