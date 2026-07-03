# Vulcan — File Explanations

Plain-English map of the codebase. Claude Code: keep this current — after creating or
materially changing any file, add/update its entry here (path + one paragraph a
non-expert can follow: what it does, why it exists, what talks to it).

## Project root

- **CLAUDE.md** — The project brief Claude Code reads at the start of every session:
  what Vulcan is, the architecture, the dimension-safety rules, and coding conventions.
- **EXPLANATIONS.md** — This file. The human-readable map of every file in the repo.
- **README.md** — Quickstart for humans: how to install, run the API, and run tests.
  (M2: setup now says Python 3.11+, since 3.13 is confirmed working — cadquery 2.8.0 has
  native 3.13 wheels.)
- **requirements.txt** — The Python libraries the project depends on, pinned loosely.
  (M3: added `openai` alongside `anthropic` — both vision SDKs, used only by
  `api/vision_provider.py`. M4: added `replicate` — the depth SDK, used only by
  `api/depth_provider.py`.)
- **.gitignore** — Tells git which files never to store (secrets, caches, generated
  exports). (M3: added `data/`, where intent JSON files live — generated at runtime,
  never committed, same treatment as `exports/`.)
- **.env.example** (M3, reworked M4) — Documents every environment variable the app
  reads: the intent parser's provider switch (`VISION_PROVIDER` + key) and (M4) the
  optional depth prior (`DEPTH_PROVIDER=none|replicate`, `DEPTH_MODEL`,
  `REPLICATE_API_TOKEN`). **M4 fix:** every comment is now on its OWN line — an inline
  `KEY=   # comment` is a python-dotenv landmine (it loads the comment text AS the
  value, which would make an "empty" key look set and defeat the startup fail-fast
  checks). Copy to `.env` (gitignored) and fill in for real.

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

## api/ (M1, extended M2, M3, and M4)

- **api/main.py** — Creates the FastAPI application. Defines `GET /health` (a trivial
  liveness check), wires in the `/designs`, `/templates`, and `/intents` routes, and
  serves two directories of static files: `exports/` (generated part files, at
  `/exports/...`) and `web/` (the test UI, at `/`). The web UI mount is registered last
  so it doesn't swallow the API routes. **M3:** added a `lifespan` startup hook that
  calls `vision_provider.check_provider_configured()`. **M4:** the hook also calls
  `depth_provider.check_provider_configured()` — a no-op for `DEPTH_PROVIDER=none` (the
  default), so depth stays fully optional, but a fail-fast if `DEPTH_PROVIDER=replicate`
  without a token. The hook only fires on a real ASGI startup (uvicorn, or
  `with TestClient(app) as c:`), never a plain `TestClient(app)`, so it can't get in the
  way of tests that mock the providers and don't have real keys.
- **api/photo.py** (M4) — The tiny `PhotoInput` container (bytes + mime type) shared by
  both provider seams. Lives in its own neutral module so `api/vision_provider.py` and
  `api/depth_provider.py` can both accept the same type without importing each other;
  re-exported from `api/vision_provider` so existing `from api.vision_provider import
  PhotoInput` imports keep working.
- **api/designs.py** — Defines `POST /designs`, the one endpoint that turns a
  `template_id` + parameter JSON into a physical part. (M2) It no longer hardcodes which
  templates exist — it looks the template up via `templates_lib.registry.get_template`,
  which every template module populates just by being imported (see
  `templates_lib/__init__.py`). Validates the params through that template's pydantic
  model, builds the CadQuery solid, exports it, and returns a design_id plus download
  URLs. Bad params or an unknown template_id come back as a clear 4xx error instead of a
  stack trace.
- **api/templates.py** (M2, refactored M3) — Defines `GET /templates`, which describes
  every registered template's parameter form (name, type, min/max, choices, default,
  description) so the web UI can build the right form for whichever template the user
  picks without any template-specific code on the frontend. **M3:** the field-extraction
  logic moved to `api/param_schema.py` (shared with the intent parser); this file is now
  just the HTTP layer over it.
- **api/param_schema.py** (M3) — `form_fields_for(model)`: turns a template's pydantic
  params model into a plain field list (name/type/min/max/choices/default/description).
  Extracted from `api/templates.py` once `api/intents.py` needed the exact same
  information — for the web form there and for telling the vision LLM what
  parameters/ranges each template accepts here. A template's `Field(...)` definitions
  stay the single source of truth for both.
- **api/vision_provider.py** (M3) — The one file in the whole codebase allowed to import
  `openai` or `anthropic` or know either provider's name (enforced by
  `tests/test_vision_provider.py::test_no_other_module_imports_provider_sdks`, which
  greps the repo). Exposes one function, `parse_intent(photos, annotation, text,
  template_catalog, *, retry_feedback=None) -> dict`; everything else — which provider,
  which model, how to shape the request, how to unwrap the response — is decided
  entirely inside this module from the `VISION_PROVIDER`/`VISION_MODEL` env vars (loaded
  via `python-dotenv`). Switching providers is one `.env` edit + a server restart, no
  code changes anywhere else. Both providers are asked to fill
  `schemas/intent_spec.schema.json`, just through each one's own structured-output
  mechanism: OpenAI gets a strict-mode-compliant transform of that schema
  (`_to_openai_strict_schema` — every object needs `additionalProperties: false` and
  *all* properties listed in `required`, derived programmatically so it can't drift from
  the canonical schema) passed as `response_format`; Anthropic gets the schema almost
  as-is as a forced tool call (`tool_choice` pins it to one tool, so the model can't
  reply with prose instead). `check_provider_configured()` is a fail-fast check — see
  `api/main.py`'s startup hook. The actual schema *validation* of what comes back is
  NOT this module's job — see `api/intents.py`. **M4 fix:** every SDK call AND every
  response-parse step (empty content, unexpected shape, non-JSON body) is now wrapped so
  the ONLY exception that can escape this module is `VisionProviderError`, carrying a
  human-readable cause (auth / quota / rate-limit / model-not-found / bad-image /
  network — mapped from the exception's `status_code` + message by `_humanize_provider_error`
  without importing any SDK's error classes). That guarantees `api/intents.py` can always
  turn a provider failure into a clean 502, never a bare 500.
- **api/depth_provider.py** (M4) — The depth analogue of `api/vision_provider.py`, and
  the one file allowed to import `replicate` (enforced by
  `tests/test_depth_provider.py::test_only_depth_provider_imports_replicate`). Exposes one
  function, `estimate_scale(photo, regions) -> list[ScaleEstimate]`, that turns
  overlay regions (an arrow/line on the photo) into real-world sizes in mm. Backend is
  chosen by `DEPTH_PROVIDER`: `none` (default) returns nothing — the whole product works
  fully without depth — and `replicate` runs a metric monocular-depth model. The metric
  geometry (`_region_size_mm`: pinhole back-projection of the two endpoints using the
  depth map + focal length) is a pure, unit-tested function, so it's correct independent
  of which model supplies the depth. **Key limitation, documented in the module:** no
  public Replicate wrapper currently returns raw metric depth — the popular Depth Pro
  wrapper discards the meters + focal length and returns only a colorized visualization —
  so this module defines the output *contract* it needs (per-pixel metric depth + focal
  length) and raises a clear `DepthProviderError` rather than inventing numbers if a model
  returns a plain visualization image. Every SDK/network/decode failure is wrapped as
  `DepthProviderError`.
- **api/intents.py** (M3) — `POST /intents` and `POST /intents/{id}/answers`: the
  photo(s)+annotation+text → IntentSpec → answered-dimensions pipeline. Builds a
  `template_catalog` from the live template registry (id, category, critical_dims,
  params) so the vision provider knows exactly what templates/params exist, calls
  `vision_provider.parse_intent()`, validates the result against
  `schemas/intent_spec.schema.json` with the `jsonschema` library, and retries once
  (with the validation error appended to the prompt) if it fails — a second failure
  becomes a 502, not a silently-wrong IntentSpec. This is also the one place that
  enforces CLAUDE.md's non-negotiable rule: `_apply_critical_dim_gate` recomputes
  `critical` (from `templates_lib.registry`'s `critical_dims`, in both directions —
  forcing it true for real critical dims and false for anything the provider
  mismarked) and `status` (⁠`ready_for_design` only once every critical dimension is
  `source="user_measured"`⁠) from scratch every time the IntentSpec changes, rather than
  ever trusting the provider's own opinion of either. If a critical dimension or its
  question is missing from the provider's output entirely, this function synthesizes
  them rather than silently allowing the gate to be skipped. Persists intents as one
  JSON file per intent under `data/intents/<intent_id>.json` — no database yet, per
  CLAUDE.md. Answers can be `measure_mm` (sets `value_mm`+`source=user_measured`),
  `confirm` (accepts an already-assumed value as measured), or `choice` (v0 scope: only
  wired up to update `material_suggestion` — mapping other enum template params from a
  choice answer is M5's job, once IntentSpec dims get joined to a full template).
  **M4 additions:** (1) after the vision pass, `_apply_depth_prior` asks
  `depth_provider.estimate_scale` for a metric size for each dim still on source
  "assumed" and, where it gets one, turns it into a `depth_inferred` suggestion (value +
  honest confidence) — critical dims still require a real `user_measured` answer, so this
  only prefills the UI's "looks like ~X — measure to confirm"; a depth-provider failure
  degrades to no proposals rather than breaking intent creation. (2) `_cross_check_measurement`
  implements CLAUDE.md rule 3: a `measure_mm` answer that disagrees with the depth prior
  by >20% is NOT committed — it records `cross_check {depth_value_mm, ratio,
  status:"mismatch_reask"}` and (re-)asks a question naming both values plus the likely
  unit slip (cm vs mm, inch vs mm); re-submitting the *same* value is an explicit override
  that commits it (status `"ok"`); with no depth prior everything commits normally (status
  `"unavailable"`). The user's number is never silently replaced by the depth value in
  either direction. The depth prior is kept stable across answers via
  `cross_check.depth_value_mm`, so a corrected re-answer is checked against the same prior.
  A `confirm` answer is routed through the SAME cross-check (and refuses to touch a dim
  currently flagged as a mismatch), so it can't become a back door that commits a
  disputed value without the re-ask.
- **api/rendering.py** — Takes a finished CadQuery solid and writes it to disk in every
  format the rest of the product needs: STEP (for manufacturing/slicing), 3MF and STL
  (for 3D printing), and a PNG preview. The preview is rendered by loading the exported
  STL's triangle mesh with `trimesh` and drawing it with `matplotlib` — deliberately not
  a live CAD viewport, so it renders correctly with no display or GPU on a server. Also
  exposes `mesh_is_watertight`, the manifold check the test suite and (later) DFM
  validation both rely on.

## templates_lib/ (M1, extended M2 and M3)

- **templates_lib/__init__.py** (M2) — Importing this package registers every template.
  Each template module registers itself as a side effect of being imported; this file's
  only job is to import all of them, so anything that needs the full registry populated
  just needs to `import templates_lib` first (api/designs.py, api/templates.py, and
  api/intents.py all do this).
- **templates_lib/registry.py** (M2, extended M3) — The template registry: a small,
  deliberately dumb module with no knowledge of any specific template (it never imports
  a template module, to avoid a circular import — templates import *it*, not the other
  way round). `TemplateSpec` bundles everything the API and test suite need to treat a
  template generically: its id, human label, pydantic params model, build function, a
  `min_wall_violation` params override used by the shared test suite, and (M3)
  `category` (the `schemas/intent_spec.schema.json` category enum value this template
  belongs to) and `critical_dims` (the param names that are fit-critical per CLAUDE.md's
  dimension rules — `api/intents.py`'s critical-dim gate reads this, never a hardcoded
  or provider-supplied list). `register_template` / `get_template` / `all_templates` are
  the whole API.
- **templates_lib/constants.py** (M2) — `MIN_WALL_MM` (2.4mm, the PETG-printable minimum
  from CLAUDE.md), factored out once a third template proved it was genuinely shared
  rather than bracket-specific. All three templates import it from here instead of each
  defining their own copy.
- **templates_lib/bracket_shelf_l.py** — The first parametric template: an L-shaped
  shelf bracket. `BracketShelfLParams` (a pydantic model, now with real defaults on
  every field — see M2 note below) validates every input — simple range checks (span,
  depth, thickness, screw count) plus cross-field geometry checks that reject
  combinations that can't physically be built (too many screw holes for the available
  arm length, thickness too large for the span). `build_bracket` is the pure function
  CLAUDE.md requires: params in, a CadQuery solid out, no I/O or global state. It builds
  the L profile, adds 1–3 triangular corner gussets depending on `load_hint`, then cuts
  the wall-mounting screw holes. **M2 fix:** the screw-hole margin previously left only
  ~half the hole's clearance radius between a hole's edge and the part's edge — below
  `MIN_WALL_MM` for #8/#10 screws. The margin (and the corresponding depth_mm validator)
  now guarantee `MIN_WALL_MM` of material at the tightest edge; see
  `test_screw_holes_respect_min_wall_to_edges`. **M2:** every field now has a real
  default (matching the original UI defaults), so `BracketShelfLParams()` with no
  arguments is itself one valid example — used by the shared test suite and by
  `GET /templates` to prefill the web form. **M3:** registered with
  `category="bracket"` and `critical_dims=("span_mm", "depth_mm")`, per this
  milestone's instructions.
- **templates_lib/adapter_tube.py** (M2) — A tube/hose adapter joining two circular
  ends (`od_a_mm`/`id_a_mm` at end A, `od_b_mm`/`id_b_mm` at end B), per
  docs/vulcan-product-spec.pdf Appendix A's `adapter.tube` entry — which only sketches
  parameter *names*, so the ranges/defaults/DFM rules here are our own engineering
  judgment. Built as a solid of revolution: an outer (OD) and inner (bore) silhouette
  sharing the same z-breakpoints are each revolved 360° into a solid, then the bore
  solid is cut from the outer solid, leaving a hollow tube open at both flat end faces
  (air/fluid passes straight through). `engagement_a_mm`/`engagement_b_mm` are the
  constant-diameter sections at each end; `taper` picks between a smooth conical
  transition between the two diameters or an abrupt stepped shoulder. Validates that
  id < od and wall thickness ≥ `MIN_WALL_MM` at both ends, that each engagement length is
  a plausible multiple of its end's OD (not a tiny stub or a fragile noodle — our own
  judgment call), and that total length stays under the spec's 250mm v1 size ceiling.
  **M3:** registered with `category="adapter"` and `critical_dims=("od_a_mm", "id_a_mm",
  "od_b_mm", "id_b_mm")` — all four diameters, per this milestone's instructions.
- **templates_lib/knob_appliance.py** (M2) — A replacement appliance control knob, per
  Appendix A's `knob.appliance` entry (again, names only — no numbers). A cylindrical
  knob body gets a bore cut into its bottom face sized to the control shaft
  (`shaft_dia_mm` + a fixed +0.2mm printed-fit clearance, per this milestone's spec —
  expect this to be recalibrated once real prints/outcomes exist), to depth
  `shaft_depth_mm`. `shaft_type` picks the bore shape: `round` (plain circle), `D`
  (circle with one flat chord cut — the common D-shaft knob bore), or `spline`
  (approximated in v0 as a regular `spline_count`-sided polygon bore — noted as a
  limitation in the module docstring, since a real spline has curved-flank teeth).
  `grip_style="ribbed"` adds 16 vertical exterior ridges; `pointer=True` adds a raised
  radial fin on the top face as a dial-position indicator. Validates radial wall
  (knob OD to bore) and top-cap wall (knob height to bore depth) both ≥ `MIN_WALL_MM`.
  **M3:** registered with `category="knob"` and `critical_dims=("shaft_dia_mm",
  "shaft_depth_mm")`, per this milestone's instructions.

## web/ (M1, rebuilt M2, extended M3 and M4)

- **web/index.html** — **M3:** now two tabs. "Start with a photo" (the new default) is
  the intent-parser flow: photo upload, freehand-annotation canvas, description text,
  a questions panel with photo overlays, and a result panel with the raw IntentSpec +
  a "Generate part" button. "Direct template params" is the M1/M2 flow unchanged — a
  template-picker dropdown, an (empty) parameter-form container `app.js` fills in, a
  preview pane, and download links. Static HTML with no framework or build step, per
  CLAUDE.md.
- **web/app.js** — The "Direct template params" tab's logic, unchanged since M2:
  fetches `GET /templates`, renders a parameter form purely from the field list (no
  template-specific code), and POSTs to `/designs` on submit. **M3:** gained the
  tab-switching click handler at the top of the file (shared across both tabs) — its
  `templateSelect`/`previewImg`/`renderDownloads`/etc. globals are also reused directly
  by `intents.js`'s "Generate part" button rather than duplicating preview/download
  rendering there. **M4 fix (b):** added shared `describeFetchError(response)` and
  `errorText(err)` helpers used by both `app.js` and `intents.js`. `describeFetchError`
  turns any failed response into a useful, always-non-empty message that includes the
  HTTP status, whether the body was JSON `{detail}`, plain text, or empty; `errorText`
  guarantees a caught error never renders as an empty string. Together they make an empty
  "Error:" impossible.
- **web/intents.js** (M3) — The "Start with a photo" flow. Lets the user upload up to 3
  photos (only the first is annotatable in this test UI — a documented v0 UI
  simplification, not a backend one) and draw a freehand polyline on it via canvas
  pointer events, recording normalized [0,1] coordinates. Submits photos + annotation +
  text to `POST /intents`, then renders `questions[]` as SVG overlays (arrows/circles,
  using the same normalized coordinates) positioned over the photo, with an input per
  question matched to its `kind` (number input for `measure_mm`, checkbox for
  `confirm`, `<select>` for `choice`). Submitting answers POSTs to
  `/intents/{id}/answers` and re-renders both the questions panel (so already-answered
  ones show as confirmed) and the result panel (raw IntentSpec + status). Once
  `status === "ready_for_design"`, "Generate part" builds a `/designs` params object —
  for each of the target template's fields, the matching dimension's `value_mm` if
  present, else the template's own default — switches to the "Direct template params"
  tab, syncs its dropdown/fields to match, and reuses its existing preview/download
  rendering. **M4:** a `measure_mm` question whose dim came back `depth_inferred` shows a
  "looks like ~X mm — measure to confirm" placeholder; a dim in `mismatch_reask` renders
  an amber warning card naming both the entered and depth values with a one-click "Yes,
  my measurement is right" button (which re-submits the flagged value → server commits it
  as an override). Question rows are now deduped per dim (a dim can carry both its
  original and a server-added `reask-` question) so inputs/cards don't double-render, and
  the answer-submit path is extracted into a shared `submitAnswers(answers)` used by both
  the button and the re-confirm.
- **web/style.css** — Styling for the test UI. **M3:** added tab styling, the
  photo/canvas/SVG-overlay layered layout (`#annotate-wrap`/`#overlay-wrap`, absolutely
  positioned canvas and SVG over the photo), question-row styling, and the
  `<pre>`-formatted IntentSpec JSON display. **M4:** added the `.mismatch-card` /
  `.reconfirm-btn` styles for the cross-check warning.

## tests/ (M1, extended M2, M3, and M4)

- **tests/test_bracket_shelf_l.py** — Tests the bracket template in isolation (no
  API/HTTP involved): every generated mesh is manifold for each `load_hint`, wall
  thickness below the printable minimum is rejected, out-of-range parameters are
  rejected, geometrically-impossible combinations are rejected, and STEP/3MF/STL all
  export as non-empty files. **M2:** added
  `test_screw_holes_respect_min_wall_to_edges`, a regression test for the min-wall
  margin fix — asserts every hole keeps ≥`MIN_WALL_MM` of material on both the top-edge
  side and the front/back-face side, for all three screw sizes.
- **tests/test_api_designs.py** — Tests the HTTP layer with FastAPI's `TestClient`
  (built on httpx): `/health` responds, a full `/designs` round-trip returns working
  download URLs whose files actually fetch with content, invalid/conflicting params
  come back as 422, and an unknown `template_id` comes back as 400. **M2:** added
  `test_templates_endpoint_lists_all_registered_templates` (every registered template
  appears with a well-formed field list) and
  `test_designs_round_trip_for_every_template`, parametrized over the live registry so
  every template — not just the bracket — gets exercised through the real HTTP API.
- **tests/template_test_helpers.py** (M2) — Template-agnostic check functions shared by
  every template's test coverage: `assert_mesh_is_manifold`, `assert_min_wall_violation_rejected`,
  `assert_all_exports_non_empty`. Not a test module itself (the name doesn't match
  pytest's `test_*.py` pattern) — `tests/test_template_suite.py` wires these up as
  parametrized tests.
- **tests/test_template_suite.py** (M2) — Runs the shared checks above against every
  template in `templates_lib.registry.all_templates()`. Because it iterates the live
  registry rather than a hardcoded list, a future template gets this baseline coverage
  automatically the moment it registers itself — no test file to remember to update.
- **tests/test_adapter_tube.py** (M2) — Geometry sanity checks specific to the tube
  adapter, beyond the shared suite: the bore is a genuine through-hole (checked two
  ways — mesh Euler number 0, i.e. torus-like/genus-1 topology, for both `taper=True`
  and `taper=False`; and mesh volume is meaningfully less than an equivalent solid built
  from the same outer silhouette with no bore cut), plus each cross-field validator
  (id≥od, wall below minimum, implausible engagement ratio, oversized total length)
  actually rejects the bad params it's meant to catch.
- **tests/test_knob_appliance.py** (M2) — Geometry sanity checks specific to the knob:
  a deeper bore removes strictly more material than a shallow one (proves
  `shaft_depth_mm` actually drives the geometry); a D-shaft knob has strictly *more*
  remaining volume than an otherwise-identical round-shaft knob (a D-bore is a circle
  with a chord cut off, so its area — and thus how much it removes — is smaller than a
  full circle; if the flat weren't really being cut, the two volumes would be
  identical); all three `shaft_type`s produce manifold meshes; both wall validators
  (radial, top-cap) reject params that violate them.

- **tests/test_vision_provider.py** (M3) — Tests `api/vision_provider.py` with both SDK
  clients mocked at their constructor (no real network). Covers provider/model
  selection from env vars, the fail-fast `check_provider_configured` check, and — the
  milestone's required conformance test — feeding the *same* underlying JSON payload
  through both the OpenAI and Anthropic adapters and asserting byte-for-byte identical
  output. Also verifies the OpenAI strict-schema transform is fully compliant at every
  nesting level (not just the top), and includes a repo-wide grep
  (`test_no_other_module_imports_provider_sdks`) that fails if any file other than
  `vision_provider.py` ever imports `openai` or `anthropic` — turning the "one file only"
  rule into something enforced, not just documented. **M4:** added tests that every SDK
  exception (auth/quota/rate-limit/model-not-found/bad-image/generic) and every bad
  response (non-JSON, empty content) becomes a `VisionProviderError` with the right
  human-readable cause — so a provider failure can always become a clean 502.
- **tests/test_depth_provider.py** (M4) — Tests `api/depth_provider.py` with no network.
  Covers provider/model selection, the fail-fast check (replicate-without-token, and
  whitespace-only token), the `none` path returning no estimates, the pure metric
  geometry against synthetic depth maps (a known pixel span at a known depth/focal gives
  the analytically-correct mm; size scales with depth; single-point/invalid regions
  return `None`; confidence stays modest and capped), the Replicate decode path with
  `replicate` mocked (a visualization PNG is rejected, a metric `.npz` + focal length
  decodes to the right size, an API error is wrapped), and the grep test that only
  `depth_provider.py` imports `replicate`.
- **tests/test_intents.py** (M3, extended M4) — Tests `api/intents.py` with
  `api.intents.parse_intent` (and, for M4, `api.intents.estimate_scale`) mocked — no
  network. Covers the create → answer → `ready_for_design` round-trip; the
  schema-validation retry path; answer `source`/`confidence` handling; the critical-dim
  gate (including a real bug it once caught — the gate now also corrects a false-positive
  `critical=true` from the provider). **M4 additions:** a provider error becomes a clean
  502; a depth prior turns an assumed dim into `depth_inferred` (and a depth-provider
  failure degrades gracefully); and the full cross-check matrix — the mm/cm slip (10x),
  the inch slip (25.4x), a re-confirmed override, a corrected value, depth-unavailable
  committing with status `"unavailable"`, and an explicit "never silently overrides the
  user" check.
- **tests/fixtures/intents/** (M3) — Ground-truth fixtures for `scripts/eval_intents.py`:
  `manifest.json` (fixture list: photos, text, optional annotation, ground-truth
  template_id/category/measured dimensions) + `photos/` + a `README.md` documenting the
  schema and how to add the 10 real photos this milestone calls for. Ships with 2
  synthetic placeholder fixtures (flat-color PIL-generated images, clearly labeled as
  placeholders) so the structure and the eval script are exercisable now; real photos
  replace them later.

## scripts/ (M3, extended M4)

- **scripts/eval_intents.py** — `python scripts/eval_intents.py --provider {openai|anthropic}`.
  Boots its own `uvicorn api.main:app` subprocess with `VISION_PROVIDER` (and, M4,
  `DEPTH_PROVIDER`) set to the requested backends (the standard "one env var + a restart"
  switch, automated so backends can be compared one command each without hand-editing
  `.env`), runs every fixture in `tests/fixtures/intents/manifest.json` through a real
  `POST /intents` call — no mocking — and reports template match rate, dimension MAE
  (mm) vs. ground truth, and critical-dim question coverage. **M4:** with
  `--depth-provider replicate` it also reports depth-proposal MAE (error of the
  `depth_inferred` values vs ground truth) and the cross-check catch rate — for each
  critical dim with a depth prior it injects a 10x-too-small answer (a cm-for-mm slip)
  and counts how many the >20% cross-check flagged. This is how M3's exit bar
  (`docs/ROADMAP.md`) and M4's cross-check behavior get measured.

## .claude/launch.json (M1)

- **.claude/launch.json** — Tells the Claude Code preview tooling how to start the dev
  server (`.venv/bin/uvicorn api.main:app --port 8000`) so UI changes can be checked in
  a real browser during development. Not used by the app itself.
