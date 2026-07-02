# Vulcan — File Explanations

Plain-English map of the codebase. Claude Code: keep this current — after creating or
materially changing any file, add/update its entry here (path + one paragraph a
non-expert can follow: what it does, why it exists, what talks to it).

## Project root

- **CLAUDE.md** — The project brief Claude Code reads at the start of every session:
  what Vulcan is, the architecture, the dimension-safety rules, and coding conventions.
- **EXPLANATIONS.md** — This file. The human-readable map of every file in the repo.
- **README.md** — Quickstart for humans: how to install, run the API, and run tests.
- **requirements.txt** — The Python libraries the project depends on, pinned loosely.
- **.gitignore** — Tells git which files never to store (secrets, caches, generated exports).

## schemas/

- **schemas/intent_spec.schema.json** — The single most important data shape in Vulcan:
  the structured interpretation of "what the user wants." Every photo+text submission
  becomes one of these. It carries the guessed part category, every dimension with its
  source (user-measured vs. inferred vs. assumed) and confidence, and the list of
  measurement questions to ask the user. The API validates against this schema.

## docs/

- **docs/vulcan-product-spec.pdf** — The full product specification (v0.2): what Vulcan
  is, how the order pipeline works, fulfillment phases, unit economics, 90-day plan.
- **docs/ROADMAP.md** — The build sequence as a numbered list of Claude Code milestones,
  each with its exit criteria. Work top to bottom; don't skip ahead.

## api/ (M1)

- **api/main.py** — Creates the FastAPI application. Defines `GET /health` (a trivial
  liveness check), wires in the `/designs` routes, and serves two directories of static
  files: `exports/` (generated part files, at `/exports/...`) and `web/` (the test UI,
  at `/`). The web UI mount is registered last so it doesn't swallow the API routes.
- **api/designs.py** — Defines `POST /designs`, the one endpoint that turns a
  `template_id` + parameter JSON into a physical part. It looks up the template in
  `TEMPLATE_REGISTRY` (currently just `bracket_shelf_l`), validates the params through
  that template's pydantic model, builds the CadQuery solid, exports it, and returns a
  design_id plus download URLs. Bad params or an unknown template_id come back as a
  clear 4xx error instead of a stack trace. Adding a template in M2 means adding one
  line to `TEMPLATE_REGISTRY` here.
- **api/rendering.py** — Takes a finished CadQuery solid and writes it to disk in every
  format the rest of the product needs: STEP (for manufacturing/slicing), 3MF and STL
  (for 3D printing), and a PNG preview. The preview is rendered by loading the exported
  STL's triangle mesh with `trimesh` and drawing it with `matplotlib` — deliberately not
  a live CAD viewport, so it renders correctly with no display or GPU on a server. Also
  exposes `mesh_is_watertight`, the manifold check the test suite and (later) DFM
  validation both rely on.

## templates_lib/ (M1)

- **templates_lib/bracket_shelf_l.py** — The first parametric template: an L-shaped
  shelf bracket. `BracketShelfLParams` (a pydantic model) validates every input —
  simple range checks (span, depth, thickness, screw count) plus cross-field geometry
  checks that reject combinations that can't physically be built (e.g. too many screw
  holes for the available arm length, or thickness too large for the span). thickness_mm
  is bounded below by `MIN_WALL_MM` (2.4mm, the PETG-printable minimum from CLAUDE.md).
  `build_bracket` is the pure function CLAUDE.md requires: params in, a CadQuery solid
  out, no I/O or global state. It builds the L profile, adds 1–3 triangular corner
  gussets depending on `load_hint` (more gussets for heavier expected loads), then cuts
  the wall-mounting screw holes.

## web/ (M1)

- **web/index.html** — The v0 test UI: a form for the bracket's six parameters, a
  preview pane, and download links. Static HTML with no framework or build step, per
  CLAUDE.md — it exists purely to exercise the API as it grows.
- **web/app.js** — Reads the form, POSTs it to `/designs`, and on success shows the
  returned preview PNG and STEP/3MF/STL download links; on failure shows the API's
  error message inline instead of failing silently.
- **web/style.css** — Minimal styling for the test UI (two-panel layout, form
  controls, download link chips). Purely cosmetic — no behavior lives here.

## tests/ (M1)

- **tests/test_bracket_shelf_l.py** — Tests the template in isolation (no API/HTTP
  involved): every generated mesh is manifold/watertight for each `load_hint`, wall
  thickness below the printable minimum is rejected, out-of-range parameters are
  rejected, geometrically-impossible combinations (screw holes that don't fit, walls
  too thick for the span) are rejected, and STEP/3MF/STL all export as non-empty files.
- **tests/test_api_designs.py** — Tests the HTTP layer with FastAPI's `TestClient`
  (built on httpx): `/health` responds, a full `/designs` round-trip returns working
  download URLs whose files actually fetch with content, invalid/conflicting params
  come back as 422, and an unknown `template_id` comes back as 400.

## .claude/launch.json (M1)

- **.claude/launch.json** — Tells the Claude Code preview tooling how to start the dev
  server (`.venv/bin/uvicorn api.main:app --port 8000`) so UI changes can be checked in
  a real browser during development. Not used by the app itself.
