# Vulcan Build Roadmap — Claude Code Milestones

Work top to bottom. One milestone ≈ one Claude Code session ≈ one git branch.
Each milestone ends with: tests passing, EXPLANATIONS.md updated, founder review.
The web test UI must stay working at every milestone — it is how progress is verified.

## M1 — Skeleton + first template + test UI  ← START HERE
FastAPI app with `/health`; CadQuery template `templates_lib/bracket_shelf_l.py`
(params: span, depth, thickness, screw size/count, rib count from load hint);
`POST /designs` (params JSON → STEP/3MF/STL + PNG render); static web page (served by
FastAPI) with a parameter form → preview image → download links. Tests: manifold,
min-wall, param ranges, exports open.
**Exit: change a number in the browser, see the bracket change, download and slice it.**

## M2 — Second + third templates
`adapter_tube.py`, `knob_appliance.py` per Appendix A of the spec. Refactor shared
template plumbing (base class, param schema loader, DFM rule runner) only now, once
three concrete examples exist. Web UI: template picker.
**Exit: 3 templates via UI; shared test suite runs against all.**

## M3 — Intent parser (photos + text → IntentSpec)
`POST /intents`: accepts photos + optional sketch/annotation + text; calls Claude
(vision) with strict JSON output matching `schemas/intent_spec.schema.json`; returns
IntentSpec incl. questions[] with photo overlays. Web UI: upload flow, questions
rendered as arrows/circles on the photo, answer boxes. Fixtures: 10 test photos with
known ground truth; measure accuracy.
**Exit: 8/10 fixture cases produce correct template + sensible questions.**

## M4 — Depth prior + cross-check
Depth service client (Depth Pro-class via Replicate or local GPU); fill
`depth_inferred` values + confidence; implement the >20% mismatch re-ask. Unit tests
for the cross-check logic incl. mm/cm/in mistake cases.
**Exit: unit-mistake demo — type 25cm where photo says 2.5cm, get re-asked.**

## M5 — Intent → design join + honest preview
`ready_for_design` IntentSpec flows into template params; preview render with
dimension callouts; critical-dim gate enforced (cannot proceed with unconfirmed
critical dims — test this).
**Exit: photo → questions → confirmed dims → rendered part → downloadable files, all in the browser.**

## M6 — Quotes, orders, Stripe, fulfillment queue
Slicer CLI integration (grams/minutes → price via spec §5 formula); `POST /quotes`,
`POST /orders` + Stripe Checkout; founder fulfillment dashboard (queue, per-order QA
card, status transitions); `POST /orders/{id}/outcome` fit-outcome records.
**Exit: a real order placed, paid, printed, marked shipped; outcome row recorded.**

## M7 — Friendly-user launch hardening
Auth-lite (email magic link), rate limits, error states, S3 (or local) file storage,
basic analytics, "we can't make that" logging endpoint + demand log view.
**Exit: 10 real orders from friendly users without founder touching the database.**

Later (post-90-day): partner-farm routing adapter, Track B freeform generation behind
founder review, native app w/ ARKit scale, B2B API keys.
