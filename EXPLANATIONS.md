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
  measurement questions to ask the user. The API validates against this schema. **M5:**
  question items gained `suggested_value` (the vision provider's recommended value for a
  choice/enum param) and `chosen_value` (filled when the user answers a choice) — both
  nullable strings, so the OpenAI strict-schema transform stays compliant. These carry
  enum-param decisions through to the intent→design join. **M7:** added top-level
  `template_fit` (0-1, how well the matched template can actually make the part) and
  `unsupported_features[]` (things the request needs the template can't express) to drive
  freeform routing; and the question `overlay` gained the dimension-line `kind`s
  (`dim_line`, `dim_ellipse` with center/rx/ry/rotation for diameters seen in perspective,
  `dim_depth`) — the legacy `shape` (arrow/circle/line) is kept for backward compatibility.

## docs/

- **docs/vulcan-product-spec.pdf** — The full product specification (v0.2): what Vulcan
  is, how the order pipeline works, fulfillment phases, unit economics, 90-day plan.
- **docs/ROADMAP.md** — The build sequence as a numbered list of Claude Code milestones,
  each with its exit criteria. Work top to bottom; don't skip ahead.

## api/ (M1, extended M2, M3, M4, M5, and M5.5)

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
  way of tests that mock the providers and don't have real keys. **M-B:** includes the
  `review` router BEFORE the `/exports` mount (so its gated download route takes precedence
  over the static files), and imports `api.freeform` for its side effect (registering the
  ephemeral-template rehydration loader on `templates_lib.registry`). **Production UI:**
  adds `CORSMiddleware` so the `web/` site can be hosted on a different origin and still
  call the API — the allowlist is `VULCAN_CORS_ORIGINS` (comma-separated, default `*`), with
  `allow_credentials=False` because the founder token is a request header, not a cookie
  (that's what makes a `*` allowlist safe). CORS does not weaken the `/exports` download gate.
  The lifespan hook also LOGS the effective vision provider (to `uvicorn.error`, so it shows in
  the console). **The recurring "I set .env=openai but it still uses anthropic" trap is now fixed
  at the source:** the provider modules load `.env` with `override=True` (see
  `api/vision_provider.py`), so `.env` is AUTHORITATIVE — its value wins over a stale/exported
  shell variable. (`env_shadowing` remains as a tested diagnostic but is no longer needed at
  startup.)
- **api/photo.py** (M4) — The tiny `PhotoInput` container (bytes + mime type) shared by
  both provider seams. Lives in its own neutral module so `api/vision_provider.py` and
  `api/depth_provider.py` can both accept the same type without importing each other;
  re-exported from `api/vision_provider` so existing `from api.vision_provider import
  PhotoInput` imports keep working.
- **api/designs.py** — Defines `POST /designs` and (M5) the shared `build_design(template_id,
  params, *, source_map=None)` internals that BOTH the direct endpoint and the intent→design
  join (api/intents.py) call, so the export pipeline lives in exactly one place. It looks the
  template up in `templates_lib.registry` (M2), validates the params, builds the CadQuery
  solid, computes the preview's dimension callouts from the template's `callouts_fn` (labelling
  each with its value + a source marker — measured ✓ / suggested ~ / default — from the
  optional `source_map`), exports STEP/3MF/STL + the annotated PNG, and returns a design_id
  plus URLs. Bad params or an unknown template_id come back as a clear 4xx error. **M-B:**
  `_produce_files` dispatches by spec type — Track A templates build a solid in-process and
  export it; freeform (`EphemeralTemplateSpec`) templates build in the sandbox subprocess
  (their code never runs in-process) and the preview is rendered here from the sandbox's STL.
  Both return the same file dict, so the runtime manifold gate and URL construction are
  identical for both. **M5.5 —
  runtime manifold gate:** right after export it re-checks the actually-written STL with
  `rendering.mesh_is_watertight`; a non-watertight (unprintable) mesh is refused with a 500
  and its half-baked export directory is deleted, so no unbuildable STL/STEP can ever be
  downloaded. The per-template pytest suite proves watertightness for DEFAULT params; this
  guards the live path where user-resolved (and, later, generated) params run. **M9.1:** each
  part is also gated to a SINGLE connected body (no floating pieces). **Audit fix (finding 1):**
  after export, `_measure_and_gate_shipped` re-gates and MEASURES the exact final artifact that
  ships — `part.stl` for a single part, or the merged `assembly.stl` for an assembly (which
  legitimately has one body per part, so it's gated to expect exactly `len(parts)` bodies) —
  and enforces the size ceiling on it. `build_design` now returns this `shipped_dfm` as a third
  value so the design record's DFM describes what SHIPS (this design's user params), not the
  generation-time build with default params. This closes a divergence where a record could say
  bbox [60,90,34]/1-body while the shipped mesh was [60,140,78]/2-body — the user-params build
  had split. The generation DFM is kept on the record as `dfm_generation` for provenance.
- **api/raster.py** (M10b) — The shared software Z-BUFFER rasterizer both previews render
  through. Vectorized numpy, NO OpenGL/pyrender/GPU: `render_mesh` projects triangles, then
  fills each one over its pixel bounding box with barycentric weights and a PER-PIXEL z-test
  using perspective-correct depth (interpolated 1/z), so nearer surfaces correctly cover
  farther ones — fixing the old painter's-algorithm mis-ordering of self-occluding parts and
  assemblies. Renders at 2× and box-downsamples for anti-aliasing. Optional SCENE OCCLUSION
  (a per-pixel scene inverse-depth culls part pixels behind the scene) and a silhouette edge
  line. `face_shades` gives per-face flat shading; the caller owns base colour. The
  interlocking-boxes regression in `tests/test_raster.py` asserts the z-order is correct and
  order-independent.
- **api/composite.py** (M5.5; M10b overhaul) — The in-photo preview: renders the ACTUAL
  generated geometry back into the user's own photo, scale- and position-true, as an OPAQUE
  ember solid with a glowing orange border. **M10b** rebuilds the drawing on the shared
  z-buffer (`api/raster.render_mesh`) so self-occluding parts and assemblies render correctly
  (no more painter's-order artifacts / internal-face bleed), and adds: (1) a soft CONTACT
  SHADOW (`_contact_shadow`) — a blurred dark ellipse below a surface mount, or the silhouette
  cast behind a wall mount, opacity scaled by how much of the frame the part fills; (2) a
  LIGHTING MATCH (`scene_lighting`) — the part's brightness is scaled toward the scene's median
  luminance around the anchor (clamped 0.6–1.3) with a subtle warm/cool tint (≤±10%), so it
  sits in the photo's light while staying recognizably ember (tint, don't repaint); (3) real
  SCENE OCCLUSION when a whole-scene depth map is supplied (`render_composite(scene_depth_mm=)`
  → the z-buffer tests the part against it so foreground objects cover it; absent → drawn fully
  in front). Pure numpy + Pillow + trimesh, still headless. Poses the part with a textbook
  pinhole camera (focal from EXIF 35mm-equivalent, else 60° FOV); placement anchors the
  centroid at the annotation centroid (or photo center); scale prefers metric depth at that
  point (`depth_provider.depth_mm_at`), else the part-vs-annotation size, else a frame
  fraction; orientation is a canonical 3/4 pose by mounting category (wall vs surface) — NOT
  recovered from the photo (honest v0 limit). The camera math (`pinhole_project`,
  `transform_to_camera`, `canonical_rotation`, `focal_px`) stays pure + unit-tested.
  `render_composite` also saves the plain (EXIF-corrected) photo as `photo.png` next to the
  composite so the UI can toggle the part in/out. `api/intents.py`'s design join calls it
  best-effort, now also passing a bounded full-scene depth map for occlusion.
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
  stay the single source of truth for both. **Expandable ranges:** each field now also
  carries `recommended_min`/`recommended_max`/`hard_reason` (read from a Field's
  `json_schema_extra`) — the softer typical range the UI shows and lets the user expand
  past, plus a reason for a limit that genuinely can't be crossed. `minimum`/`maximum`
  remain the HARD buildable limits (pydantic ge/le).
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
  human-readable cause. **Overlay guidance (tightened):** the SYSTEM_PROMPT tells the
  model to add a question's photo overlay ONLY when it can actually locate the feature —
  set `overlay` to null rather than emit a misplaced marker (LLMs localize in 2D poorly,
  and a wrong arrow misleads more than none) — to prefer a `circle` at the feature's
  center (the UI draws a generous ring, so pixel precision isn't needed), to make
  arrow/line endpoints the real points whose distance IS the measurement, and to anchor
  overlays on the user's own annotated region when one was provided. This keeps the
  red-circle/arrow guides but reduces the "confidently wrong" placements. The prompt also
  now requires every `measure_mm` question to set `dim_name` to a template param and to
  ONLY ask about the chosen template's own numeric params (no invented extra measurements)
  — `api/intents.py` still handles the stragglers, but this reduces them at the source.
  **M10a (visual critique):** this seam also exposes `critique_design(render_paths,
  request_text, params, param_schema)` — it sends 4 canonical renders of a generated part to
  the vision model and returns `{matches_request 0–1, defects[], targeted_fixes[]}` (same
  provider selection + `VisionProviderError` contract). This is the "eyes" of the freeform
  best-of-N loop.
  **M7:** the prompt now demands routing honesty — it sets `template_fit` (0-1) and lists
  `unsupported_features` the template can't express, and is told that shoehorning a bad fit
  is worse than admitting "other" (fixing the greedy router). The overlay instructions were
  replaced: each critical measurement emits a DIMENSION-LINE overlay of the right `kind`
  (`dim_line` for a linear span, `dim_ellipse` — center/rx/ry/rotation — for a diameter of a
  round thing seen in perspective, `dim_depth` for a receding measurement), with no value on
  the overlay (the UI fills the label from the dimension's own honest state).
  human-readable cause (auth / quota / rate-limit / model-not-found / bad-image /
  network — mapped from the exception's `status_code` + message by `_humanize_provider_error`
  without importing any SDK's error classes). That guarantees `api/intents.py` can always
  turn a provider failure into a clean 502, never a bare 500. **`.env` is authoritative:** this
  module (and depth/codegen) call `load_dotenv(override=True)`, so the value in `.env` wins over
  a stale/exported shell `VISION_PROVIDER` — the fix for the recurring "my .env says openai but
  it keeps using anthropic" trap. It's safe: tests set env vars via monkeypatch AFTER import (so
  they still win), `.env` doesn't set `DEPTH_PROVIDER`/`CODEGEN_PROVIDER` (so the test CLI
  overrides stand), and a deployment with no `.env` is a no-op (OS env wins). **env_shadowing():**
  a now-diagnostic-only helper — compares the effective `VISION_PROVIDER` against the `.env` value
  and returns `(os_value, dotenv_value)` when they differ (else None). Covered by
  `tests/test_vision_provider.py` (override-wins, shell-shadows-.env, agree, no-shell-var, key-absent, case/
  whitespace).
- **api/depth_provider.py** (M4; M10c local) — The depth analogue of `api/vision_provider.py`,
  and the one file allowed to import a depth backend — `replicate`, or (M10c) `torch`/
  `depth_pro` for local — enforced by
  `tests/test_depth_provider.py::test_only_depth_provider_imports_depth_backends`. Exposes one
  function, `estimate_scale(photo, regions) -> list[ScaleEstimate]`, that turns
  overlay regions (an arrow/line on the photo) into real-world sizes in mm. Backend is
  chosen by `DEPTH_PROVIDER`: `none` (default) returns nothing — the whole product works
  fully without depth. **M10c — `local`:** Apple's open-source Depth Pro running IN-PROCESS
  (`_run_local_model` → `_load_local_model`): torch + depth_pro are imported ONLY here and
  ONLY lazily (so the base install stays light and the seam holds), the model loads once and
  is cached for the process, its ~1.9 GB weights auto-download from Hugging Face
  (`apple/DepthPro`) on first use with one clear log line, and it estimates the focal length
  itself. Device is auto-picked (Apple-Silicon MPS > CUDA > CPU, override `DEPTH_LOCAL_DEVICE`).
  `local` and `replicate` satisfy the SAME contract — `_run_depth_model` dispatches to either
  and returns `(depth_map_meters, fx, fy)` — so `estimate_scale`, `depth_mm_at`, and
  `depth_map_mm` all work identically for both. The metric geometry (`_region_size_mm`:
  pinhole back-projection of the two endpoints using the depth map + focal length) is a pure,
  unit-tested function, so it's correct independent of which model supplies the depth.
  **Replicate caveat, documented in the module:** no public Replicate wrapper currently returns
  raw metric depth — the popular Depth Pro wrapper discards the meters + focal length and
  returns only a colorized visualization — so the replicate path defines the output *contract*
  it needs (per-pixel metric depth + focal length) and raises a clear `DepthProviderError`
  rather than inventing numbers if a model returns a plain visualization image; `local`
  sidesteps this by reading Depth Pro's raw metric output directly. Every SDK/network/decode
  failure is wrapped as
  `DepthProviderError`. **M5.5:** adds `depth_mm_at(photo, x, y)`, a best-effort metric
  depth (mm) at a single normalized image point, used by the ghost composite to place the
  part at the true distance of the circled surface. Unlike the rest of the module it NEVER
  raises and returns `None` whenever depth is unavailable (`DEPTH_PROVIDER=none` or any
  model failure) — a preview must not be able to break because a depth backend hiccuped.
  **M10b:** adds `depth_map_mm(photo)`, the best-effort FULL-scene depth map (HxW, mm) the
  composite z-tests the part against for occlusion; invalid/background pixels come back as
  `+inf` (never occlude), and like `depth_mm_at` it returns `None` (never raises) when depth
  is unavailable — so occlusion degrades gracefully to drawing the part in front. **M10c:**
  `check_provider_configured` fails fast for `local` too — it requires torch + depth_pro to be
  importable (`_local_available`), so a misconfigured server says so at startup instead of at
  first depth call.
- **requirements-local.txt** (M10c) — The OPTIONAL heavy deps for `DEPTH_PROVIDER=local`
  (torch, torchvision, huggingface_hub, and Apple's `depth_pro` from git), kept OUT of the base
  requirements so a normal install/CI stays light. Pins `numpy>=2` to override depth_pro's stale
  `numpy<2` metadata (it runs fine on numpy 2, which the rest of the stack needs). Install with
  `pip install -r requirements-local.txt`, then set `DEPTH_PROVIDER=local`.
- **scripts/depth_smoke.py** (M10c) — A one-photo smoke test for the local provider:
  `DEPTH_PROVIDER=local python scripts/depth_smoke.py <photo>` runs Depth Pro through the
  api.depth_provider seam (it never touches torch/depth_pro directly) and prints the
  center-pixel depth + scene range. First run downloads the weights; a clear non-zero exit if
  the local stack isn't installed.
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
  them rather than silently allowing the gate to be skipped. Relatedly,
  `_ensure_measure_question_dimensions` gives EVERY `measure_mm` question a usable
  `dim_name` + a matching (non-critical) dimension: vision models sometimes ask the user
  to measure something with a `dim_name` that's absent from `dimensions[]` (e.g.
  `gap_wall_to_tap_mm`) or with no `dim_name` at all (an invented extra measurement like
  `q_wall_to_faucet_center`) — both used to 422 when answered. Missing names are derived
  from the question id via `_derive_dim_name` (`q_wall_to_faucet_center` →
  `wall_to_faucet_center_mm`) and the dimension is synthesized; `_apply_answer` does the
  same derivation/creation on demand as a backstop for older stored intents. (The vision
  prompt was also tightened to only ask measure_mm questions for the template's own
  numeric params, so these invented measurements are rarer at the source.) Persists
  intents as one
  JSON file per intent under `data/intents/<intent_id>.json` — no database yet, per
  CLAUDE.md. Answers can be `measure_mm` (sets `value_mm`+`source=user_measured`),
  `confirm` (accepts an already-assumed value as measured), or `choice` (v0 scope: only
  wired up to update `material_suggestion`; **M5** finished the deferral so a choice answer
  now maps to ANY enum template param — it records `chosen_value` on the question).
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
  disputed value without the re-ask. **M9:** the join takes an optional body
  `DesignJoinRequest{fulfillment: "files"|"ship"}` (default "files", bad value → 422) — how the
  customer wants it delivered, chosen at submit BEFORE review; it's persisted on the intent, put
  on the freeform review record, and returned so the founder dashboard can show it. **M5
  addition:** `POST /intents/{id}/design` is the
  intent→design join — the endpoint that closes the loop. It refuses with a 409 unless the
  intent is `ready_for_design` (so the critical-dim gate holds end to end), then
  `_resolve_design_params` maps the IntentSpec onto the template's FULL param set:
  dimension values by source (a critical param may only come from `user_measured` — a
  defensive 409 if that's ever violated — while `depth_inferred`/`assumed` are fine for
  non-critical ones), and each enum/boolean param resolved as answered-choice
  (`chosen_value`) > provider suggestion (`suggested_value`) > template default. It calls
  `designs.build_design` (reusing the export pipeline), persists the intent→design link,
  and returns the files plus a per-param summary (value + source) that drives the UI table.
  **M5.5 additions:** (1) `POST /intents` now PERSISTS the uploaded photos (under
  `data/intents/<intent_id>/photos/`) and stores the raw `annotation` on the intent, so the
  later join can render the ghost composite into the user's own photo — see the module
  docstring's PRIVACY note (user photos kept on disk with no expiry yet; `data/` is
  gitignored, but a real deployment needs a retention/delete policy). (2) the design join
  now calls `_render_ghost_composite`, a strictly best-effort step: when the intent has a
  stored photo it renders `composite.png` into the export dir and adds `files.composite`;
  with no stored photo it simply omits the composite, and ANY failure is logged and swallowed
  (returns `None`) so a preview problem can never block delivering the actual part files. The
  ghost's optional metric-depth lookup runs through `_bounded_depth_mm_at`, a hard wall-clock
  deadline (M5.5 review): with `DEPTH_PROVIDER=replicate` that lookup is a synchronous network
  call, and the deadline guarantees a slow/stalled depth backend can never make the design
  join hang on a *preview* (under the default `none` it returns instantly and never waits).
  **M-B (Track B):** `POST /intents` now stores the raw `request_text` and flags
  `freeform_available` when no template fits (in scope, no template_id). `POST
  /intents/{id}/freeform` runs `freeform.generate_and_register` on the stored photos + request
  (clean 502 if the codegen provider isn't configured), and on success adopts the generated
  ephemeral template so the SAME critical-dim gate synthesizes its measure questions; on
  failure it returns the intent with an honest `freeform_error` (the request is already logged
  to the demand log). The join, for a freeform intent, writes a `pending_review` design record
  (api/design_store) so it lands in the founder queue with downloads gated (api/review).
  **M10a (async freeform):** because best-of-N + visual critique is slow, `POST
  /intents/{id}/freeform` is now ASYNC — it does the cheap pre-checks, then returns `202` with
  `{job_id, status_url}` and runs generation on a background job (`api/jobs`). `GET
  /intents/{id}/freeform/{job_id}` polls the job (stage generating→critiquing→ready|failed) and,
  on ready, returns the updated intent. `_apply_freeform_outcome` (run on the job thread) adopts
  the winning template and stashes the generation-quality provenance (`freeform_critique`,
  `freeform_dim_contract`, `freeform_score`, `freeform_candidates`) on the intent — which the
  design join then copies onto the review record. **Job-loss recovery (bugfix):** the in-memory
  job registry (`api/jobs`) does NOT survive a server restart, so the poll RECOVERS from the
  intent's persisted state when the job id is unknown — `ready` if the template was attached,
  `failed` if it errored, and a clear "interrupted, please try again" (never a bare 404) if the
  run never finished. This was hit routinely under `uvicorn --reload`: a generation writes
  `data/generated_templates/<id>/…` and the dev reloader restarted the server mid-run — now the
  generated module is stored as `.cqpy` (not `.py`, see api/freeform) AND the documented run
  command scopes `--reload` to source dirs, so generation no longer restarts the server.
  **M7 (Part A routing fix):** `_apply_freeform_routing` sets `freeform_available` (true for
  any in-scope request — the always-on override) and `freeform_recommended` (true when no
  template matched, `template_fit` < 0.65, or any `unsupported_features`) so the greedy router
  no longer rides a bad-fit template silently. `POST /intents/{id}/freeform` is now the
  always-available user OVERRIDE too: it no longer 409s when a template already matched —
  instead the generated template REPLACES it, and the old template's dimensions/questions are
  cleared so the gate synthesizes a clean set for the custom design. **M7 follow-up fixes:**
  `_attach_param_bounds` puts each numeric param's bounds (mm) on the intent as
  `param_bounds`. **M8 (expandable ranges):** it now emits `{minimum, maximum, recommended_min,
  recommended_max, hard_reason}` — the HARD buildable limits (pydantic ge/le, widened on the
  Track-A templates), the softer RECOMMENDED range the UI lets the user expand past, and a
  reason for a limit that can't be crossed. The UI blocks only on the hard bounds (with the
  reason); relational rules like "the screw hole can't be bigger than the plate" stay in each
  template's `model_validator` and surface, with their message, at generate. And on a freeform override, `_freeform_questions` seeds the
  critical-dim questions WITH the overlays the codegen model placed on the photo (via
  `outcome.overlays`), so the photo shows the dimension drawing for freeform parts too — they
  used to be synthesized overlay-less.
- **api/rendering.py** — Takes a finished CadQuery solid and writes it to disk in every
  format the rest of the product needs: STEP (for manufacturing/slicing), 3MF and STL
  (for 3D printing), and a PNG preview. **M10b — the PRODUCT SHOT:** `render_studio` now
  renders the `preview.png` shown in the UI/review page (and later order emails) through the
  SAME shared z-buffer renderer as the in-photo composite (`api/raster`), for one consistent
  look: a fixed 3/4 view, a neutral vertical-gradient background, per-part palette colours
  (so an assembly's pieces match the 3D viewer), a silhouette edge line, and the dimension
  callouts projected through the same camera and drawn with Pillow. The camera auto-fits (it
  iterates on the real projected extent, then centres). `export_design` (single part) and
  `designs._produce_files_freeform` (assemblies) both emit the studio shot. The older
  matplotlib `render_preview`/`render_assembly_preview` remain for tests / as a fallback. **M5:**
  `render_preview`/`export_design` take optional `callouts` ({p0, p1, text}) and draw each
  as a labeled 3D dimension arrow (the "honest preview" — value + measured ✓ / suggested ~
  / default marker), still fully headless. Also
  exposes `mesh_is_watertight`, the manifold check the test suite, the **M5.5** runtime
  manifold gate (`api/designs.build_design`), and (later) DFM validation all rely on — it
  loads with `force="mesh"` and treats empty geometry as not-watertight, so a pathological
  export can't load as a `Scene` (no `.is_watertight`) and throw past the gate's cleanup
  (M5.5 review hardening); it stays fail-closed. **M9:** adds `heal_mesh_file` — the manifold
  gate WITH auto-repair. If the exported mesh isn't watertight it tries a light, print-safe
  fix (merge coincident vertices, fix winding + normals, `fill_holes` via networkx) and, if
  that makes it watertight, OVERWRITES the STL with the healed mesh so the shipped part is
  manifold; a genuinely broken mesh (real holes/open faces) still returns False and is
  rejected. `api/designs.build_design` and `api/freeform.dfm_check` both call it, so a valid
  generated solid whose tessellation had hairline gaps is no longer wrongly rejected as
  "not manifold". A no-op for already-watertight meshes (every template default).
  **M9.1:** adds `mesh_body_count` (how many DISCONNECTED bodies the mesh has — a real part
  is ONE connected solid; >1 = floating/disjoint pieces, which watertightness alone misses
  because two disjoint closed bodies are each watertight) and `write_preview_mesh` (a coarse,
  decimated `part_preview.stl` for the 3D viewer — low-poly enough to serve UNGATED so a
  customer can orbit their part while the full-res STEP/3MF/STL stay download-gated; needs
  `fast-simplification`, falls back to the full mesh if absent). `api/designs.build_design`
  now rejects a design that isn't watertight OR isn't a single body, and emits a `view_stl`
  URL for the preview mesh; `api/freeform.dfm_check` adds `connected`/`body_count` to its
  report and its feedback so the self-repair loop is told to fuse floating pieces.
  **M9.2 (multi-part assemblies):** a freeform design may now be an ASSEMBLY of several separate
  parts that fit together (a lid+box, a peg+socket). `render_assembly_preview` renders them in
  one PNG each a distinct `PART_PALETTE` colour; `write_preview_mesh(out_name=...)` makes a
  per-part ungated view mesh. `build_design` gates EACH part (single connected manifold) and
  returns a `parts` list `[{name, step, stl, threemf, view_stl, color_index}]`; for a multi-part
  design it also merges the parts into `assembly.stl` (for the in-photo composite + a combined
  view fallback). Single-part designs keep the flat top-level `step/stl/threemf/view_stl`.
  **M5.5:** `render_preview` now pads any zero-thickness bounding-box axis before setting the
  3D limits, so a degenerate/flat mesh (e.g. a broken template producing a single planar
  face) renders instead of crashing matplotlib's projection — the manifold gate is what then
  rejects such a part, with a clean message rather than a traceback from the preview step.
  **M10a:** adds `render_canonical_views(stl_paths, out_dir)` — 4 fixed camera angles
  (`CANONICAL_VIEWS`: iso/front/side/top, colored per part) written as PNGs, the "eyes" the
  freeform visual-critique loop feeds to `vision_provider.critique_design`. Same headless
  trimesh+matplotlib path; freeform serializes calls to it (pyplot is process-global and
  best-of-N renders in threads).

### Track B — freeform generation (M-B)

Track B lets the LLM AUTHOR a one-off parametric template when no registry template fits.
Everything flows through the EXISTING machinery: the generated artifact becomes a
dynamically-registered template, so the same intent flow (questions → user_measured gate →
join → runtime manifold gate) builds it. Generated code is untrusted and executed ONLY in a
sandbox; every freeform design requires founder review before its files can ship.

- **api/code_verifier.py** (M-B) — The first, most important safety layer: a static AST check
  on model-authored code. Leaf module (imports only `ast`) so the API and the sandbox
  subprocess can both use it. Allows imports of ONLY `cadquery`/`math`/`numpy` and a
  `build(params)` entrypoint; rejects (collecting EVERY violation, for good feedback) any
  other import, `exec`/`eval`/`compile`/`open`/`__import__`/`getattr`-style names, and any
  dunder-attribute/name access (`__globals__`, `__subclasses__`, `__builtins__`, …) — the
  classic escape vectors. Fails closed. It is static analysis, not a full sandbox — layered
  with runtime guards + OS isolation below.
- **api/sandbox.py** (M-B) — The containment boundary. `run_generated_build(code, params,
  out_dir)` verifies the code, then runs it in a SEPARATE `python -I` subprocess (isolated
  mode) with a hard wall-clock timeout, OS resource limits (CPU/file-size/#files/#procs via
  setrlimit), stdin/stdout/stderr on /dev/null, and an environment scrubbed of secrets. The
  subprocess exports STEP/STL/3MF into its own temp dir; only those geometry files are copied
  back — no generated Python re-enters the API process. **M9.2:** `build(params)` may return one
  Workplane OR a dict/list of several parts (an assembly); each is exported to its OWN
  `<name>.{step,stl,3mf}` and `SandboxResult.parts` carries one entry per piece. Part names come
  from model output and BECOME filenames, so they're sanitized to `[a-zA-Z0-9_]` in the runner
  AND re-validated against a strict allowlist in `_collect_outputs` (defense-in-depth; a
  `../evil` name can't escape out_dir). Capped at 8 parts. Build failures (bad geometry, timeout)
  come back as `SandboxResult(ok=False, stage=...)` (never an exception) so the self-repair
  loop can learn from them. Honest limitation, documented in the module + security notes: this
  is static-analysis + process-isolation + rlimits, NOT a syscall jail (no container/seccomp);
  on macOS some rlimits aren't enforced (the timeout is the primary bound there). **M10a
  (dimensional contract):** `run_generated_build` takes an optional `probe_params` (a list of
  perturbed full-param sets); the runner builds each and measures it, and the measurements
  (`{baseline, probes}` each with bbox/volume/area) ride back on `SandboxResult.measurements`.
  The CPU rlimit now scales with the wall-clock timeout (`_make_preexec`), since a probe-heavy
  build legitimately takes longer.
- **api/_sandbox_runner.py** (M-B) — The tiny script that runs INSIDE that subprocess (never
  imported into the API process). Re-verifies the code (defense in depth), then `exec`s it in
  a locked-down namespace — a guarded `__import__` admitting only cadquery/math/numpy and
  builtins with `open`/`eval`/`exec`/`compile` removed — calls `build(params)`, exports the
  three formats, and writes `result.json`. The untrusted code's stdout/stderr are silenced.
  **M10a:** after exporting, it MEASURES the built solid straight from the CadQuery shapes
  (`_measure_parts`: union bbox extents, total volume, total surface area — no extra export),
  and if `probes.json` lists alternate param sets it rebuilds under each and measures those too
  (measure-only), so the parent can verify every declared length param actually moves the
  geometry. Measurements go into `result.json`.
- **api/codegen_provider.py** (M-B) — The code-generation seam (same shape as
  `vision_provider`/`depth_provider`): `generate_template(request, photos, dims_hints,
  retry_feedback=None)` returns `{cadquery_code, param_schema, assumptions, critical_dims}`.
  Backend chosen by `CODEGEN_PROVIDER` (openai|anthropic, default openai), `CODEGEN_MODEL`
  override. THE ONLY module besides `vision_provider` allowed to import openai/anthropic (the
  isolation grep test now permits both). The system prompt states the DFM rules (min wall
  2.4mm, 250mm ceiling, FDM constraints) and shows two exemplar templates as style references;
  the output uses a hand-authored strict JSON schema, and `param_schema` matches
  `form_fields_for()`'s shape. Does NOT execute or DFM-check code — that's freeform's job.
  **M7 follow-up:** the prompt now insists on GENEROUS param min/max ranges (a user's real
  measurement is rejected if it falls outside, so ranges that were too tight caused 422s at
  the join), and the output gained an `overlays` array — a dimension overlay (dim_line/
  dim_ellipse/dim_depth) per critical dim locating it on the photo, which `api/freeform` and
  `api/intents` carry through so freeform questions draw dimension lines like Track A ones.
  **M9.1/M9.2:** the prompt demands each returned piece be ONE connected watertight solid (no
  floating fragments) and function-first geometry, and allows returning a dict of SEPARATE named
  pieces when the hardware genuinely needs an assembly (positioned where they mate, with a
  printing clearance, NOT fused). `freeform.dfm_check_parts` runs the gate on every piece and
  `_dfm_feedback` names the failing piece for the self-repair loop. **M10a:**
  `generate_template` also accepts retrieved `exemplars` (approved designs from
  `api/exemplar_store`) and renders them as EXTRA few-shot references in the user prompt.
- **api/freeform.py** (M-B; M10a rewrite) — Orchestrates generation with COMPETITION + EYES +
  MEMORY. Round 1 is BEST-OF-N: `BEST_OF_N` candidates are generated IN PARALLEL
  (`ThreadPoolExecutor`) and each taken through validate → sandbox build (+ dimensional probes)
  → DFM → **dimensional contract** → **visual critique**, then scored (gates + dim-contract +
  critique); the highest scorer wins. Rounds 2–3 regenerate ONE candidate with the winner's
  feedback (critique fixes or the gate error) appended — the retry budget is `MAX_ATTEMPTS`
  rounds. **Dimensional contract** (`build_dimensional_probes` + `dimensional_contract_check`):
  perturb each length param (`_mm`), rebuild in the sandbox, and require the built solid's bbox
  OR volume to actually change; a param that moves nothing is a DEAD param and hard-fails with a
  param-named self-repair message (so we never ask a user to measure a dimension the code
  ignores). **Visual critique** (gated by `VULCAN_CRITIQUE`, default on): renders the winner
  candidate from 4 canonical views and asks `vision_provider.critique_design` for a
  `matches_request` 0–1 score + defects + fixes; below `CRITIQUE_THRESHOLD` (0.7) → regenerate.
  Still normalizes param_schema → pydantic model, keeps `dfm_check`, self-repair, demand log,
  persistence under `data/generated_templates/<id>/` (the generated module is stored as `.cqpy`,
  NOT `.py`, so writing it can't trip a dev `uvicorn --reload` and restart the server mid-job —
  it's read back as text + sandbox-exec'd, never imported; `_code_path` still reads legacy
  `code.py`), and rehydration. On success persists full
  provenance (every candidate's code+scores, all critiques, the dimensional-contract detail).
  If geometry is valid but critique never clears the bar after all rounds, the best shippable
  candidate STILL ships (founder review is the backstop). Takes an optional `progress` callback
  (used by the async job to report generating→critiquing). **Audit fix (finding 2):** provenance
  now records `critique_enabled` (top-level) and `critique_ran` (per candidate + on the winner),
  so a 0.7-default critique score is unambiguous — you can tell "critique off" from "enabled but
  skipped/errored" instead of inferring it from an identical 0.91 score. (The prior bridge runs
  that showed identical 0.91s were the TEST suite with critique intentionally off, not a live
  skip; a new test proves the async job path runs real critique when enabled.)
- **api/embedding_provider.py** (M10a) — The text-embedding seam (same shape as the other
  provider seams; on the SDK-isolation allowlist because the OpenAI path imports `openai`).
  `embed()`/`embed_one()`; `EMBEDDING_PROVIDER` (openai|anthropic|local, default openai). openai
  hits the real endpoint (`text-embedding-3-small`); anthropic (no native embeddings) and local
  both use `local_embedding` — a deterministic, offline, dependency-free signed feature-hashing
  of the text's tokens into an L2-normalized vector, good enough to rank request similarity (and
  what the tests use). Only the openai path raises.
- **api/exemplar_store.py** (M10a) — Approved-design few-shot MEMORY. On approval (see
  `api/review._remember_exemplar`) every freeform design is stored under `data/exemplars/<id>.json`
  as `(request_text, param_schema, code)`. `retrieve(request_text, k)` embeds the query + every
  stored request via `embedding_provider` (falling back to the local embedding if the provider is
  offline) and returns the top-k by cosine similarity — fed to codegen as extra exemplars.
  `add_exemplar` is idempotent per design_id. Empty store → `[]` (codegen then uses its two
  static exemplars).
- **api/jobs.py** (M10a) — In-process async job registry (a dict + lock + daemon threads) backing
  the now-async freeform endpoint. `start(intent_id, target)` runs `target(progress)` on a
  background thread (or INLINE when `VULCAN_JOBS_SYNC` is set — tests use this so a provider mock
  stays active and polling is deterministic), updating stage generating→critiquing→ready|failed;
  `get_job` is what the poll endpoint reads. Right-sized for the single-founder Phase-0 deploy; a
  multi-worker deploy would swap in a real queue without changing the API surface. The registry is
  in-memory (lost on restart) — the poll endpoint recovers a finished run from the intent on disk
  (see api/intents `get_freeform_job`), so a restart doesn't strand the client on a 404.
- **api/design_store.py** (M-B) — One JSON record per freeform design under `data/designs/`
  (request, generated code, resolved params, DFM results, files, and the founder's verdict +
  note). Only freeform designs get a record; Track A designs don't (so they're never gated).
  This record set is also the templatization-mining corpus. **M10a:** records also carry the
  generation-quality provenance (visual `critique`, `dim_contract`, overall `score`, and the
  best-of-N `candidates`) for the review page. **Audit fix:** the record's `dfm` is now the
  SHIPPED-artifact measurement (from `build_design`), with the generation-time build kept as
  `dfm_generation`; and `critique_enabled` records whether critique was on for the run.
- **api/review.py** (M-B) — The founder review queue + the download gate. `GET /review`
  lists records (pending by default), `POST /review/{id}` records an approve/reject verdict +
  note. `GET /exports/{id}/{file}` is the gated download route — registered BEFORE the static
  `/exports` mount so it takes precedence — that returns 403 for a freeform design's CAD files
  (STEP/STL/3MF) until its record is `approved`; the preview PNG is always served, and Track A
  files (no record) pass straight through. **Security-review hardening:** there is NO static
  `/exports` mount (it let gated files leak under non-canonical spellings); this is the ONLY
  export route, it case-folds the gated-file check and rejects non-canonical paths (so
  `PART.STL`/trailing-slash/`//`/`.` can't slip a gated file out), and `POST /review/{id}`
  requires the `X-Review-Token` header when `VULCAN_REVIEW_TOKEN` is set (so downloads can't be
  self-approved). The AST verifier was also hardened (see api/code_verifier.py) after a
  red-team pass found a submodule-attribute escape, and the sandbox now SIGKILLs the child's
  whole process group on timeout. **M7 follow-up (founder downloads):** the gate exists to stop
  the CUSTOMER getting files early, not the reviewer — so the download route now accepts the
  founder's `X-Review-Token` (`_founder_authorized`) to serve a PENDING design's CAD files, and
  the review dashboard (web/review.js) shows download buttons for every design (pending =
  "Founder preview") that fetch WITH the token (kept out of the URL) and also sends the token on
  approve/reject. A plain customer request (no token) still 403s until approved. **M10a:** on
  APPROVAL, `_remember_exemplar` stores the design (request → param_schema → code) via
  `api/exemplar_store` so future generations learn from it (best-effort — never blocks approval).

## templates_lib/ (M1, extended M2, M3, M5, and M-B)

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
  or provider-supplied list). **M5:** added the `DimCallout` dataclass and a `callouts_fn`
  on `TemplateSpec` — each template declares its own preview dimension arrows (which param,
  the two 3D endpoints in part coords, a label), which `api/designs.py` resolves to
  labeled callouts for the honest preview. `register_template` / `get_template` /
  `all_templates` are the whole API. **M-B (Track B):** added `EphemeralTemplateSpec` (a
  TemplateSpec subclass carrying the generated `code`) and a SEPARATE ephemeral registry
  (`register_ephemeral_template` / `_EPHEMERAL`) so freeform templates never pollute the
  Track A catalog — `all_templates()` stays Track-A-only (GET /templates, the generic test
  suite) while `get_template` resolves both, and on a miss calls an optional
  `set_ephemeral_loader` hook so a stored freeform template rehydrates from disk after a
  restart (keeps this leaf module free of any api-layer import).
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
  milestone's instructions. **M5:** `bracket_callouts` declares the span + depth preview
  arrows. **M8 (expandable ranges):** the dimensional params' `ge`/`le` were widened to
  generous HARD limits (e.g. span 20–450) and each carries a softer `recommended_min/max` in
  `json_schema_extra` (span 40–300) plus a `hard_reason` on physical floors (thickness can't go
  below `MIN_WALL_MM`). The relational `model_validator` rules are unchanged and remain the real
  gate; the wider `ge`/`le` just let the user push a single dimension past the recommended range.
  The same widening + `recommended_*` pattern was applied to `adapter_tube.py` and
  `knob_appliance.py`.
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
  "od_b_mm", "id_b_mm")` — all four diameters, per this milestone's instructions. **M5:**
  `adapter_callouts` declares the OD + bore diameter arrows at each end.
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
  "shaft_depth_mm")`, per this milestone's instructions. **M5:** `knob_callouts` declares
  the knob-diameter, shaft-diameter, and shaft-depth preview arrows.

## web/ (M1, rebuilt M2, extended M3, M4, M5, M5.5, M-B, and Production UI)

**Production UI (dark "forge" theme + standalone client).** The site is no longer a "test
UI": it's a dark, black-canvas production site themed off the fire-V logo (ember/molten
accents, vendored Anton + Space Grotesk fonts, ambient embers, glow, a hero + "how it works"
+ studio + footer), and it's a **standalone client** — it shares no code with the API and
talks to it purely over HTTP through one module (`web/api.js`), so it can be hosted anywhere.
The rewrite preserved every DOM id, runtime-toggled class, and endpoint the app JS depends on;
only the chrome around them and the network seam changed. See `web/README.md`.

- **web/config.js** (Production UI) — Sets `window.VULCAN_CONFIG.apiBase`: `""` = same origin
  (FastAPI serves the site in dev), or an API origin for a separately-hosted frontend. Resolved
  from `window.VULCAN_API_BASE`, a `<meta name="vulcan-api-base">`, or the fallback string.
  Loaded before `api.js`.
- **web/api.js** (Production UI) — `window.VulcanAPI`, the ONE place the site talks to the API.
  Every request AND every API-served asset URL goes through it: `getTemplates/createDesign/
  createIntent/submitAnswers/freeform/joinDesign/reviewQueue/submitVerdict/fetchFile`, plus
  `url()`/`asset()` which resolve a `files.*` path (or any API path) against `apiBase`
  (absolute/`blob:`/`data:` pass through; same-origin is a no-op). Also home to the shared
  `describeFetchError`/`errorText` globals (moved here from app.js). Change `apiBase` and the
  whole site follows the API to a new origin — no other edits. **M10a:** freeform is async, so
  `freeform()` starts the job and a new `freeformJob(statusUrl)` polls it.
- **web/site.js** (Production UI) — Presentation-only chrome: nav solidify-on-scroll, mobile
  menu toggle, and reveal-on-scroll via IntersectionObserver. Fully progressive — the app flow
  never depends on it; every hook is guarded so a missing element is a no-op.
- **web/assets/** (Production UI) — The logo (`logo.png`, the fire-V) and favicons
  (`favicon.ico`, `favicon-32.png`, `apple-touch-icon.png`, `icon-192/512.png`). The logo is
  black-backed and glowing; the CSS uses `mix-blend-mode: screen` so the black square vanishes
  and only the glowing V shows on the dark page.
- **web/vendor/fonts/** (Production UI) — Vendored, offline (no CDN, per CLAUDE.md): `anton-400`
  (the VULCAN wordmark + hero display) and `space-grotesk-var` (UI/body, variable 300–700).
- **web/README.md** (Production UI) — Documents the standalone-frontend contract: how `api.js`
  is the only API seam, how to point `apiBase` at an API origin, the `VULCAN_CORS_ORIGINS`
  env, the file map, and the script load order.

- **web/review.html + web/review.js** (M-B) — The founder review page (served at
  `/review.html`, distinct from the `GET /review` API). Lists freeform design records with a
  pending/all filter; each card shows the request, the model's assumptions, the DFM/manifold
  results, the render, the resolved params, and the generated code (lightly, XSS-safely
  highlighted — only `<>&` are escaped, then token spans are added), with Approve/Reject
  buttons + a note field. Verdicts `POST /review/{id}` and drive the download gate. **M10a:**
  each card also shows the generation-quality provenance — the winner's visual-critique score +
  defects (`critiqueLine`, which now distinguishes a real score from "critique disabled" vs
  "enabled but not scored" so a 0.7 default isn't mistaken for a genuine critique — audit fix),
  the dimensional-contract summary (which length params were verified to drive geometry, or dead
  ones flagged — `dimContractLine`), and an expandable best-of-N table listing every candidate's
  stage/score/visual-match with the winner starred (`candidatesBlock`). **M7
  follow-up:** a founder-token field (localStorage-backed) is sent as `X-Review-Token` on
  approve/reject AND on downloads, and every design card shows Download STEP/3MF/STL buttons
  that fetch WITH the token (blob download, token kept out of the URL) — so the founder can
  pull a design's files straight from the dashboard, even while it's pending ("Founder
  preview"). The customer-facing result panel keeps its "🔒 locked until approved" links and
  now points to `/review.html` to approve. **M-B additions to the photo tab** (`web/index.html` + `web/intents.js` + `web/style.css`): when
  `POST /intents` returns `freeform_available` with no template, a "Custom design" panel
  offers "Design it for me" (calls `POST /intents/{id}/freeform`, shows generation progress,
  then the standard questions/preview flow), honestly labelled "needs a human check before it
  ships"; **M10a:** `runFreeform` now polls the async job and shows per-stage progress
  ("Designing candidates in parallel…" → "Reviewing the renders and picking the best design…")
  before adopting the winner. A freeform design's result shows a pending-review banner + the model's assumptions
  and renders its CAD downloads as "🔒 locked until approved" (matching the server-side 403).
  **M7 additions to the photo tab** (`web/index.html` + `web/intents.js` + `web/style.css`):
  (Part A) an always-on "Design this custom instead" OVERRIDE inside the questions panel
  (shown whenever freeform is available), with a recommend note listing the template's
  `unsupported_features` when the router flagged a poor fit; the shared `runFreeform()` drives
  both it and the no-template panel. (Part B) the question overlays are now proper DIMENSION
  drawings on the photo, styled like a RULER laid on the scene: `drawRulerLine` draws a white
  legibility halo, evenly-spaced graduation ticks, end serifs (witness marks) and outward
  arrowheads; diameters are drawn as a FULL circle/ellipse hugging the round feature with the
  measured diameter as a ruler line across it and a ⌀ label; depth is a dashed foreshortened
  line. It all goes into an SVG whose viewBox is set to the image's rendered pixel size (so
  lines/ellipses/chips are 1:1 and undistorted), with a label chip ON the line. The chip's state is HONEST and matches the source rules
  everywhere else — "?" (unanswered, red), "~210mm" (a vision/depth estimate, amber, never
  without the ~), "203.2mm ✓" (green, ONLY user_measured), or a mismatch showing both. It's a
  two-way live binding: typing in a measurement input updates its chip immediately (with unit
  conversion, as a no-✓ pending value); clicking a chip focuses its input; a submitted answer
  re-renders every chip from the server response.
  **3D-viewer follow-up (dashboard):** each review card now has a `buildCardView` view-box with
  a segmented toggle — "With part" (the composite), "Photo only" (the plain `photo.png`),
  "Dimensions" (the render), a "3D" button that mounts the interactive model, and "⛶ Expand"
  for a fullscreen modal. The 3D and modal viewers fetch the STL WITH the founder token
  (`Vulcan3D.create(..., { token: founderToken() })`), so a pending design's mesh loads even
  though its downloads are gated; at most one inline viewer + one modal viewer exist at a time
  (each mount disposes the previous, and a list reload disposes the inline one) to stay well
  under the browser's WebGL-context limit.
  **Production UI:** the page is dark-themed and gets the shared nav + logo; all of `review.js`'s
  network calls and asset URLs now route through `VulcanAPI` (`reviewQueue`/`submitVerdict`/
  `fetchFile`, and `asset()` on the card image/STL URLs) — so the dashboard is a standalone
  client too. Loads `config.js` + `api.js` before `review.js`.


- **web/index.html** — **M3:** now two tabs. "Start with a photo" (the new default) is
  the intent-parser flow. "Direct template params" is the M1/M2 flow — a template-picker
  dropdown, a parameter-form container `app.js` fills in, a preview pane, and downloads.
  Static HTML with no framework or build step, per CLAUDE.md. **M5:** the photo tab's
  result panel is now a self-contained "Your part" — a "Generate my part" button, an
  annotated-preview `<img>`, a param-summary `<table>`, download links, and the raw
  IntentSpec tucked into a `<details>` — and it loads `units.js` before `intents.js`.
  **M5.5:** the result panel leads with two side-by-side views — the in-photo ghost
  ("In your photo") and the dimensioned render ("The part") — in a `#design-views` grid;
  the ghost figure is hidden when the API returns no composite (the lone render then
  doesn't stretch, via a `:has()` rule in the CSS). **3D-viewer follow-up:** the "In your
  photo" figure gains a "With part / Photo only" toggle, and "The part" figure swaps its
  static render for a live `#viewer3d-part` (drag-orbit / scroll-zoom / right-drag-pan) with
  a "3D / Dimensions" toggle and an "⛶ Expand" button; a shared `#viewer-modal` provides the
  fullscreen viewer. It loads the vendored Three.js stack + `viewer3d.js` before the app JS.
  **Production UI:** `index.html` is now a real product page — dark theme, sticky nav with the
  fire-V logo, a hero ("Describe it. Hold it."), a five-step "how it works", the studio (the two
  tabs become a segmented control), and a footer — all wrapping the SAME functional ids so the
  flow is unchanged. It loads `config.js` + `api.js` before `app.js`/`intents.js` (which now call
  `VulcanAPI`), and `site.js` last. A few labels moved to production copy ("Analyze my photo",
  "Forge my part").
- **web/viewer3d.js + web/vendor/{three.min.js,STLLoader.js,OrbitControls.js}** — The
  interactive 3D model viewer and its vendored dependencies (Three.js r128 UMD + the matching
  STLLoader/OrbitControls, which attach to the global `THREE`). Vendored, not CDN-loaded, so
  the test UI keeps working offline with no build step (CLAUDE.md). `Vulcan3D.create(container,
  stlUrl, { token, fallbackImg })` fetches the STL (optionally with the founder `X-Review-Token`
  so a gated pending design still renders on the dashboard), parses it, stands the CAD Z-up part
  up, and drives a damped-orbit WebGL scene with framing to the bounding box; it returns a handle
  with `dispose()` (cancels the RAF, disconnects the ResizeObserver, frees the GL context and
  removes the canvas) and `resetView()`. If the library or the fetch fails (e.g. a 403 on a gated
  STL with no token), it shows `fallbackImg` (the render) instead — so the customer panel, which
  has no token, degrades gracefully for pending freeform parts. **M9.2:** adds
  `Vulcan3D.createAssembly(container, parts, opts)` for a multi-part design — `parts` is
  `[{url, colorIndex, name}]`; it loads each piece, colours it from `PART_COLORS` (matching the
  server palette), and on load ANIMATES the pieces from an exploded layout into their assembled
  positions (each piece pushed outward along its direction from the assembly centre, eased back
  to 0); the handle gains `replay()`. `create`/`createAssembly` share one scene-setup helper.
  `web/intents.js` and `web/review.js` use `createAssembly` (and render per-part downloads +
  a "▶ Replay" button) when `design.files.parts` has more than one entry.
- **web/units.js** (M5) — The ONE place lengths are converted (CLAUDE.md rule 4). Pure
  helpers: `toMm(value, unit)` (mm/cm/in → mm), `formatDual` ("8 in = 203.2 mm"), and a
  session-remembered unit (`get/setSessionUnit`). Internal units stay mm everywhere; this
  converts right at the input boundary. Verified in-browser (and by a node sanity check).
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
  "Error:" impossible. **Production UI:** `describeFetchError`/`errorText` MOVED to `api.js`
  (shared by both flows); app.js's `GET /templates`/`POST /designs` now go through
  `VulcanAPI.getTemplates()`/`createDesign()`, and preview/download URLs through
  `VulcanAPI.asset()`.
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
  the button and the re-confirm. **M5:** every `measure_mm` input now gets a mm/cm/in unit
  selector + live dual display (via `appendMeasureField`), and `collectMm` converts to mm
  before sending. Each choice `<select>` shows the provider's "use suggested: X" default
  and reflects an earlier `chosen_value`. "Generate my part" now calls the join endpoint
  `POST /intents/{id}/design` and `renderDesign` shows the annotated preview, a param
  summary table (value + source badge), and the STEP/3MF/STL downloads — the user never
  touches the raw template form. **M5.5:** `renderDesign` also fills the "In your photo"
  ghost image from `files.composite` (with a cache-buster) when present and hides that
  figure when it's absent, so a re-generated design never shows a stale composite.
  **3D-viewer follow-up:** `renderDesign` records `compositeSources = {with: composite,
  without: photo}` and wires the "With part / Photo only" toggle (`setCompositeView`), and it
  mounts the interactive model via `showPartView`/`mountPartViewer` (`Vulcan3D.create`), with
  the "3D / Dimensions" toggle, the "⛶ Expand" modal, and Escape-to-close; switching away from
  3D or re-rendering disposes the prior viewer so only one WebGL context is live at a time.
  **Production UI:** all four endpoints go through `VulcanAPI` (`createIntent`/`submitAnswers`/
  `freeform`/`joinDesign`), and every `design.files.*` URL used as an `<img src>`, download
  `href`, or the 3D viewer's STL is wrapped in `VulcanAPI.asset()` — so the whole flow works
  from a separately-hosted site. **Measurement UX (M8):** (1) every measurement is labelled with
  a NAME (`dimLabel`, prettified from `dim_name`) shown BOTH as a `.question-name` on the form
  field and as the top line of its on-photo chip (chips are now two-line: name + live value).
  (2) Ranges are SOFT: `appendMeasureField` shows a RECOMMENDED range you can expand past
  (`recRange`); a value beyond it but still buildable gets a non-blocking amber nudge, and only a
  genuinely-HARD limit/rule blocks — with its reason — via `hardBlockReason` (the relational
  rules stay in the templates' `model_validator`s and surface at generate). (3) Overlays are
  redrawn to look painted INTO the photo: `bowedPath`/`quadAt` bend the line with the surface,
  `drawTube` layers a dark base + molten body + bright highlight with a blurred cast shadow
  (`dim-cast`), `drawDimLine` adds foreshortened ticks + pin `drawNub` ends + outward arrows, and
  `drawDimRing` wraps a diameter around the round feature like a band on a cylinder (bright near
  arc + faint dashed far arc + the measured-diameter tube).
  **M9:** (a) MULTI-PHOTO — every uploaded photo is shown and drawable (`annItems`, one canvas
  each, `attachDrawing`); the composite/overlay still key off the FIRST photo (`firstPhotoUrl`),
  and the submit sends one annotation entry per photo with strokes. (b) A global `#question-units`
  mm/cm/in toggle (`applySessionUnit`) drives ALL measurement fields at once, and a per-field
  change updates the toggle. (c) `makeZoomable` gives the composite + the question overlay
  scroll-to-zoom / drag-to-pan (double-click resets; clicks pass through at 1×). (d) A
  `#fulfillment-choice` (files vs ship) is captured at generate and sent to `joinDesign`.
  **M9.1:** the 3D viewer now uses the UNGATED `files.view_stl` (coarse preview mesh) when
  present, so 3D works for a pending freeform part whose real STL would 403. A `#regenerate-btn`
  appears after a design is built: for a CUSTOM (freeform) part it re-rolls a fresh model attempt
  (`runFreeform`), for a template it rebuilds via the shared `doGenerate()`. `review.js` shows the
  DFM connectivity (`1 body ✓` / `N PIECES ✗`) and its viewer also prefers `view_stl`.
- **web/style.css** — Styling for the test UI. **M3:** tab styling, the photo/canvas/SVG
  overlay layout, question rows, the IntentSpec JSON display. **M4:** the `.mismatch-card`
  / `.reconfirm-btn` cross-check styles. **M5:** the `.measure-field` (input + unit
  selector + dual display), the `#design-result` layout, the `#design-params-table` with
  colored source badges, and the design download links. **M5.5:** the two-up `#design-views`
  grid (`.design-view` figures with captions), which collapses to one column when the
  composite is hidden (`:has()`) or on narrow screens. **3D-viewer follow-up:** the `.viewer3d`
  canvas box (gradient bg, grab cursor), the `.view-toggle` segmented control (`.seg`/`.seg.active`,
  `.expand-btn`), the fullscreen `#viewer-modal` + `.viewer-modal-bar`, the `.viewer-fallback`
  image/message, and the dashboard `.review-view` / `.review-view-box`.
  **Measurement UX (M8):** the flat "ruler" overlay classes were replaced with a 3D "painted-on"
  set — `.dim-cast` (blurred shadow), `.dim-tube-base`/`.dim-tube-body`/`.dim-tube-hi` (the
  layered glowing tube, `.dim-depth` dashes a receding line), `.dim-tick`, `.dim-nub*` (end
  pins), `.dim-ring-front`/`.dim-ring-back` (the diameter band), and a two-line chip
  (`.dim-chip-name` over `.dim-chip-text`). Plus `.question-name` (the measurement's name on the
  form field) and `.range-hint.soft` (the amber "outside the typical range" nudge).
  **Production UI:** fully rewritten as the dark "forge" theme — molten palette tokens
  (`--bg` near-black, `--ember`/`--molten`/`--flame` accents), vendored Anton + Space Grotesk
  `@font-face`, the ambient `.bg-embers` layer + `.ember` particles, the `.site-nav`/`.hero`/
  `.how-steps`/`.studio`/`.site-foot` chrome, and restyled versions of every functional class
  (panels, segmented `.tabs`/`.seg`, `.viewer3d`, dim-overlay chips as a glowing ruler, param
  badges, review dashboard). A global `[hidden]{display:none!important}` guarantees the JS's
  `.hidden` toggles win over any display rule; `prefers-reduced-motion` disables the animations;
  colors were tuned to clear WCAG AA. All ids/classes the JS drives are preserved.

## tests/ (M1, extended M2, M3, M4, M5, M5.5, M-B, and Production UI)

- **tests/test_cors_and_static.py** (Production UI) — Locks in the standalone-frontend
  contract: a cross-origin GET gets an `Access-Control-Allow-Origin` header; a preflight
  OPTIONS for the token-bearing verdict POST is allowed (methods + `X-Review-Token`); the
  new `config.js`/`api.js` client seam is actually served as JS; and `index.html` loads
  `config.js`+`api.js` before `app.js`/`intents.js` (or the shared globals + `VulcanAPI`
  wouldn't exist when the flow scripts run).
- **tests/test_code_verifier.py** (M-B) — Security tests for the AST safety gate: legitimate
  CadQuery code passes; every escape shape is rejected — disallowed imports (os/sys/subprocess/
  socket/relative), banned names (open/eval/exec/`__import__`/getattr/…), dunder-attribute
  smuggling (`().__class__.__bases__[0].__subclasses__()`), dunder-name references, global/
  nonlocal, a missing `build`, and syntax errors — and ALL violations are collected, not just
  the first.
- **tests/test_sandbox.py** (M-B) — Security + behavior tests for the containment boundary: a
  valid build exports all three formats; the malicious cases (os import, file open, network
  import, dunder smuggling) are rejected at the `verify` stage WITHOUT spawning the subprocess
  and leave no side effects (e.g. no `touch`ed marker file); an infinite loop is killed by the
  timeout; a runtime build error comes back as a `run`-stage error for self-repair.
- **tests/test_codegen_provider.py** (M-B) — The codegen seam with both SDKs mocked at their
  constructor: provider/model selection, the fail-fast key check, both adapters parsing a
  mocked response into the same dict, and provider-error wrapping.
- **tests/test_freeform.py** (M-B) — The orchestration with codegen mocked but the sandbox real:
  param-schema normalization/coercion + labels, the pydantic model enforcing ge/le bounds,
  first-try success (registered + persisted), the self-repair sequence (unsafe → bad build →
  good, with retry_feedback threaded — pinned to `BEST_OF_N=1` for a deterministic sequence),
  an oversize part failing the DFM size gate, total failure logging to the demand log, and disk
  rehydration after a simulated restart. **M10a:** the dimensional contract (a dead param is
  flagged + named; internal-hole changes credited via volume; an ignored-param design is
  rejected end-to-end), best-of-N (3 candidates evaluated, one winner, losers kept in
  provenance), and the visual-critique loop (a below-0.7 critique regenerates with the fixes
  appended; critique disabled → skipped and still ships). **Audit fixes:** the shipped DFM
  measures the FINAL artifact — the user-params build's bbox matches the file on disk and
  differs from the generation-default build (finding 1); and the async JOB path runs REAL
  critique when enabled — candidates get non-default scores and provenance records
  `critique_enabled`/`critique_ran` (finding 2).
- **tests/conftest.py** (M10a) — Autouse test defaults so the suite runs offline/deterministic:
  `VULCAN_CRITIQUE=off` (critique tests opt back in + mock the provider), `EMBEDDING_PROVIDER=local`
  (deterministic embeddings), and `VULCAN_JOBS_SYNC=1` (freeform jobs run inline so a provider
  mock stays active and polling is deterministic).
- **tests/test_exemplar_store.py** (M10a) — The approved-design memory with the local embedding:
  empty store → no exemplars, retrieval ORDERING by request similarity (a bridge query surfaces
  the bridge exemplar first), and idempotent add per design_id.
- **tests/test_jobs.py** (M10a) — The async job registry with plain-callable targets (no network):
  sync inline execution → ready, an `ok=False` payload and a raised exception → failed, the
  async/background path transitioning to ready, and an unknown job id → None.
- **tests/test_review.py** (M-B) — The full mocked freeform round trip over HTTP: create an
  "other" intent → generate → measure the critical dims → join → `pending_review` record →
  the download gate (CAD files 403 while pending, preview 200) → approve → files download;
  plus reject-keeps-locked, generation-failure returns an honest error, freeform-409 when a
  template already fits, review 404s, invalid-verdict 422, and Track A downloads staying
  ungated. The `test_no_other_module_imports_provider_sdks` grep test (in
  `tests/test_vision_provider.py`) was extended to also allow `codegen_provider.py`. **M7:**
  the "already has a template" case is now `test_freeform_override_replaces_matched_template`
  (freeform is the always-on override — it replaces the matched template and clears its dims).
- **tests/test_routing.py** (M7 Part A) — Routing regression with the vision provider mocked:
  8 canned intents (lego bridge plate, curved cable guide, hole-grid shelf, two-piece clamp
  → freeform recommended; shelf bracket, tube adapter, knob, corner brace → stay on template)
  assert `freeform_available`/`freeform_recommended` from `template_fit`/`unsupported_features`.
  The real provider's routing on the same 8 is a live eval (reported in the milestone
  write-up), which scored 8/8.
- **tests/test_overlays.py** (M7 Part B) — Schema validation for the new overlay `kind`s
  (dim_line/dim_ellipse/dim_depth all validate; legacy shapes still validate; a bogus kind is
  rejected), plus a JS-free check that a mocked provider emitting the new kinds round-trips
  through `POST /intents` with the overlays intact and still schema-valid.

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
  every template — not just the bracket — gets exercised through the real HTTP API. **M5.5:**
  `test_manifold_gate_rejects_non_watertight_mesh_and_cleans_up` monkeypatches the bracket's
  `build_fn` to emit a single planar (non-watertight) face and asserts `build_design` raises
  a 500 AND leaves no export directory behind (fail closed).
- **tests/test_raster.py** (M10b) — Tests `api/raster.py`, the shared z-buffer. Headline: two
  INTERLOCKING boxes (one nearer) — the nearer box must cover the farther one at the overlap,
  AND stay correct when the far box's faces are drawn last (proving order-independence, unlike
  painter's algorithm). Also: 2× supersampling produces anti-aliased (fractional-alpha) edges,
  and `edge_rgb` draws a silhouette line.
- **tests/test_composite.py** (M5.5; M10b) — Tests `api/composite.py`, the in-photo ghost. The
  camera math is checked against ANALYTIC pinhole cases (no image diffing): a point on the
  optical axis lands on the principal point, a known offset lands at the algebraically-
  predicted pixel, a unit cube at a known pose projects symmetrically with the near face
  larger than the far, `focal_px` matches both the EXIF-35mm and assumed-FOV formulas, and
  both canonical mounting rotations are proper (orthonormal, det=1). It also covers the
  annotation centroid/extent parsing and the depth→annotation→fallback scale precedence,
  then renders end-to-end on a real bracket mesh and asserts the ghost is opaque ember. **M10b:**
  a synthetic depth map whose near half occludes the part hides that half (and absent depth
  degrades to drawing in front); the lighting match makes the part dimmer in a dark scene than
  a bright one (`scene_lighting` clamps 0.6–1.3); and a contact shadow darkens the ground
  around the part.
- **tests/template_test_helpers.py** (M2) — Template-agnostic check functions shared by
  every template's test coverage: `assert_mesh_is_manifold`, `assert_min_wall_violation_rejected`,
  `assert_all_exports_non_empty`. Not a test module itself (the name doesn't match
  pytest's `test_*.py` pattern) — `tests/test_template_suite.py` wires these up as
  parametrized tests.
- **tests/test_template_suite.py** (M2, extended M5) — Runs the shared checks above
  against every template in `templates_lib.registry.all_templates()`. Because it iterates
  the live registry rather than a hardcoded list, a future template gets this baseline
  coverage automatically the moment it registers itself. **M5:** also asserts every
  template declares valid preview callouts (each references a real field, has two distinct
  3D endpoints, and a label).
- **tests/test_rendering.py** (M5; M10b) — Tests the headless preview pipeline: `render_preview`
  always closes its matplotlib figure — even when `savefig` fails — so figures can't
  accumulate in matplotlib's global registry inside the long-lived server (regression for
  a review finding), plus `export_design` with callouts writes a non-empty annotated PNG.
  **M10b:** `render_studio` produces a product shot with solid ember part pixels over a
  top-lighter gradient background, an assembly gets distinct per-part palette colours, and an
  empty mesh still writes a background PNG (never crashes the join).
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
  `depth_provider.py` imports a depth backend. **M10b:** `depth_map_mm` returns `None` without a
  provider, converts a mocked meters map to mm marking invalid pixels `+inf`, and swallows a
  model failure to `None` (occlusion never breaks the join). **M10c (local):** provider
  selection; `_local_available` detects a missing package; the fail-fast check fires when the
  local stack is absent and passes when present; and — with the model runner MOCKED (no torch,
  no weights) — `estimate_scale`/`depth_map_mm`/`depth_mm_at` all route through the same metric
  geometry for `local`. The isolation grep now also forbids `torch`/`torchvision`/`depth_pro`
  imports outside the seam.
- **tests/test_intents.py** (M3, extended M4 and M5) — Tests `api/intents.py` with
  `api.intents.parse_intent` (and, for M4, `api.intents.estimate_scale`) mocked — no
  network. Covers the create → answer → `ready_for_design` round-trip; the
  schema-validation retry path; answer `source`/`confidence` handling; the critical-dim
  gate (including a real bug it once caught — the gate now also corrects a false-positive
  `critical=true` from the provider). **M4 additions:** a provider error becomes a clean
  502; a depth prior turns an assumed dim into `depth_inferred` (and a depth-provider
  failure degrades gracefully); and the full cross-check matrix — the mm/cm slip (10x),
  the inch slip (25.4x), a re-confirmed override, a corrected value, depth-unavailable
  committing with status `"unavailable"`, and an explicit "never silently overrides the
  user" check. **M5 additions:** the intent→design join — the 409 gate before
  ready_for_design, a choice answer recording `chosen_value`, the full precedence
  round-trip (measured dim > assumed non-critical > chosen enum > suggested enum > default,
  asserted against the generated params), fetchable downloads, and two regression tests for
  review findings: a critical dim that's `user_measured` but valueless must NOT open the
  gate, and the join must 409 rather than build a part from a template default in its place.
  **M5.5 additions:** uploaded photos are persisted under `data/intents/<id>/photos/` with
  the annotation stored on the intent (round-trip asserted), the join returns a fetchable
  `files.composite` when a decodable photo is stored, and the join still succeeds and simply
  omits the composite when no photo is present. The cleanup fixture now also removes each
  intent's photo directory. **Also:** regression tests for the "dimension not found" / "no
  dimension to measure" 422s — `_derive_dim_name` unit cases, a `measure_mm` question whose
  dimension the provider omitted is answerable, one the provider gave no `dim_name` at all
  (an invented `q_wall_to_faucet_center`) is answerable via a derived name, and a stored
  intent predating the normalization is still answerable (`_apply_answer` creates the dim on
  demand) — plus a deadline test for the ghost's bounded depth lookup (`_bounded_depth_mm_at`
  returns None instead of blocking).
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
