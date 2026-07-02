# Intent-parser evaluation fixtures

Ground-truth test cases for `scripts/eval_intents.py`, which sends each fixture
through the real `POST /intents` endpoint (real vision-provider call — no mocking)
and scores the result against what's recorded here. This is how M3's exit
criterion ("8/10 fixture cases produce correct template + sensible questions",
per `docs/ROADMAP.md`) gets measured, and how the two providers get compared on
identical inputs.

## Layout

```
tests/fixtures/intents/
  manifest.json     -- the fixture list (see schema below)
  photos/           -- one or more image files per fixture
  README.md         -- this file
```

## manifest.json schema

A JSON object with one key, `fixtures`: a list of fixture objects.

```json
{
  "fixtures": [
    {
      "id": "unique_short_name",
      "photos": ["photos/some_file.jpg"],
      "text": "What the user typed, verbatim.",
      "annotation": null,
      "ground_truth": {
        "template_id": "bracket_shelf_l",
        "category": "bracket",
        "dimensions_mm": {
          "span_mm": 150,
          "depth_mm": 40
        }
      },
      "notes": "Anything a human needs to know about this fixture."
    }
  ]
}
```

- **id** — unique, filesystem-safe (used in eval report output).
- **photos** — 1-3 paths, relative to this directory, in submission order.
  `photo_index` in `annotation` and in the returned `IntentSpec`'s
  `questions[].overlay` refers to this order.
- **text** — the free-text description to submit alongside the photo(s).
- **annotation** — optional; same shape `POST /intents` accepts: a list of
  `{"photo_index": 0, "points": [[x, y], ...]}` with normalized (0-1) coords.
  `null` if the fixture has no annotation.
- **ground_truth.template_id** — the correct `template_id`, or `null` if this
  fixture should come back `category: "other"` or `status: "out_of_scope"`
  (i.e. no template should match).
- **ground_truth.category** — the correct `category` enum value (see
  `schemas/intent_spec.schema.json`).
- **ground_truth.dimensions_mm** — a `{dim_name: true_value_mm}` map with the
  *actually measured* value for each dimension you want scored, keyed by the
  template's own param names (e.g. `span_mm`). You don't have to include every
  param the template has — only fill in ones you've measured. At minimum,
  include the template's `critical_dims` (see `templates_lib/registry.py`)
  so `eval_intents.py` can check whether the parser asked about all of them.
  Omit this key entirely (or leave it `{}`) for out-of-scope/no-template
  fixtures.
- **notes** — free text, optional. Use it to flag placeholder/synthetic
  fixtures, unusual lighting, ambiguous requests, etc.

## The two fixtures currently here

Both `001_shelf_bracket_placeholder` and `002_appliance_knob_placeholder` are
**placeholders**: flat-color synthetic images generated with PIL, not real
photos, with illustrative (not measured) ground-truth numbers. They exist so
this directory's structure and `scripts/eval_intents.py` are exercisable and
testable right now. They are not a meaningful accuracy signal — expect the
vision provider to describe them vaguely or guess dimensions with high
variance, since there's no real object to look at.

## Adding the real 10

1. Drop each photo in `photos/`.
2. Add one fixture entry per photo (or per multi-photo case) to
   `manifest.json`, following the schema above.
3. Fill `ground_truth` with dimensions you've actually measured by hand —
   this is the whole point of the harness, so don't estimate from the photo.
4. Delete or keep the two placeholders as you like; `eval_intents.py` doesn't
   care how many fixtures there are.
5. Run `python scripts/eval_intents.py --provider openai` (or `anthropic`) and
   compare the report against `docs/ROADMAP.md`'s M3 exit bar.
