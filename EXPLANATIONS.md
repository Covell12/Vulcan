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
