"""The ONE seam for freeform code generation in Vulcan (Track B).

Same shape as api/vision_provider.py and api/depth_provider.py: every other
module calls `generate_template(...)` and never imports an LLM SDK or branches
on a provider name. The backend is chosen by CODEGEN_PROVIDER
(openai|anthropic, default openai); CODEGEN_MODEL overrides the model. This file
and api/vision_provider.py are the ONLY two allowed to import openai/anthropic
(enforced by the isolation grep test).

`generate_template` asks the model to AUTHOR a one-off parametric CadQuery
template for a request no registry template fits. It returns, as a dict:
  - cadquery_code: a self-contained module defining `build(params)` (params is a
    plain dict keyed by param name) that returns a cadquery Workplane, importing
    ONLY cadquery/math/numpy (enforced later by api/code_verifier + the sandbox);
  - param_schema: the SAME field format as api.param_schema.form_fields_for()
    (name/type/minimum/maximum/choices/default/description);
  - assumptions: plain-English notes about what the model inferred;
  - critical_dims: the fit-critical param names that must be user_measured.

This module does NOT execute the code and does NOT validate DFM — it only gets
the model's best attempt back. Verification, sandboxed execution, DFM/manifold
checks, and the self-repair loop all live in api/freeform.py + api/sandbox.py.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from dotenv import load_dotenv

from api.photo import PhotoInput

# override=True: .env is authoritative over shell/OS env — see the note in
# api/vision_provider.py. Safe for tests (monkeypatch runs post-import) and
# deployments (no .env -> no-op).
load_dotenv(override=True)

_DEFAULT_MODELS = {
    "openai": "gpt-5",
    "anthropic": "claude-opus-4-8",
}

_REQUIRED_ENV_VAR = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

# From CLAUDE.md / templates_lib.constants; stated in the prompt so the model
# designs to them. Kept as literals here (not imported) so this seam module has
# no template-layer dependency.
_MIN_WALL_MM = 2.4
_MAX_BBOX_MM = 250.0

# Two exemplar templates shown to the model as STYLE references. They use the
# dict-based `build(params)` contract (not the pydantic models the real Track A
# templates use), because that is exactly the interface the sandbox calls.
_EXEMPLARS = """# EXEMPLAR 1 — an L-shaped shelf bracket.
import cadquery as cq

MIN_WALL = 2.4

def build(params):
    span = float(params["span_mm"])        # length of each L leg
    depth = float(params["depth_mm"])       # width along the shelf edge
    t = float(params["thickness_mm"])       # wall thickness (>= MIN_WALL)
    # L profile in XY, extruded along Z by depth.
    pts = [(0, 0), (span, 0), (span, t), (t, t), (t, span), (0, span)]
    solid = cq.Workplane("XY").polyline(pts).close().extrude(depth)
    # A single mounting hole, kept MIN_WALL clear of every face.
    r = 2.4
    solid = (
        solid.faces("<X").workplane(centerOption="CenterOfBoundBox")
        .pushPoints([(0, span * 0.6)]).hole(r * 2)
    )
    return solid


# EXEMPLAR 2 — a concentric tube/hose adapter (revolved solid, open bore).
import cadquery as cq

def build(params):
    od_a = float(params["od_a_mm"]); id_a = float(params["id_a_mm"])
    od_b = float(params["od_b_mm"]); id_b = float(params["id_b_mm"])
    length = float(params["length_mm"])
    # Outer and inner radius-vs-z profiles share z breakpoints so the wall is
    # well defined; revolve each, then cut the bore out of the body.
    outer = [(0, 0), (od_a / 2, 0), (od_b / 2, length), (0, length)]
    inner = [(0, 0), (id_a / 2, 0), (id_b / 2, length), (0, length)]
    body = cq.Workplane("XZ").polyline(outer).close().revolve(360, (0, 0, 0), (0, 1, 0))
    bore = cq.Workplane("XZ").polyline(inner).close().revolve(360, (0, 0, 0), (0, 1, 0))
    return body.cut(bore)
"""

_SYSTEM_PROMPT = f"""You are the freeform part designer for Vulcan, a service that 3D-prints \
custom parts on FDM printers. A request has arrived that none of our fixed templates fit. \
Your job: AUTHOR a small, self-contained parametric CadQuery program that produces the part, \
plus the parameter schema and which parameters are fit-critical.

Output contract (return ONLY the structured object you are given a schema/tool for):
- cadquery_code: a complete Python module that:
  * imports ONLY `cadquery`, `math`, and `numpy` (no other imports whatsoever — no os, sys, \
subprocess, requests, importlib, etc.; any other import will be rejected and your design \
discarded);
  * defines a top-level function `build(params)` where `params` is a plain dict keyed by \
your parameter names (e.g. params["width_mm"]); read values with float(...)/int(...);
  * returns EITHER one cadquery Workplane (a one-piece part) OR — when the hardware genuinely \
needs SEPARATE pieces that fit/move together (e.g. a lid + a box, a peg + a socket, a clamp's \
two halves, a hinge's two leaves + a pin) — a dict {{"piece_name": Workplane, ...}} of the \
distinct pieces (snake_case names; up to 8). Each piece is exported to its OWN STEP/STL/3MF \
file and shown in a different colour. POSITION every piece where it actually sits in the \
assembled product (in the same coordinate space) so they display correctly fitted together; \
add a small printing clearance (~0.2-0.4mm) between mating pieces so they aren't fused.
  * EACH returned piece must be ONE connected, watertight, closed manifold body — this is the \
#1 cause of rejection. Within a single piece, UNION every feature into that one body (make \
booleans actually OVERLAP in volume, not merely touch at a face/edge — a face- or edge-only \
contact leaves a non-manifold seam or a floating fragment); never leave a loose/near-but-\
unfused fragment, a bare sketch, or a wire. But do NOT fuse pieces that are meant to be \
separate parts — keep those as distinct dict entries. Safest pattern per piece: build the \
outer solid, `.cut()` the holes/pockets, `.union()` its own add-ons; avoid self-intersecting \
sweeps/lofts and zero-thickness walls.
  * uses NO file/network/system access, NO eval/exec, NO dunder-attribute tricks — pure \
geometry only. It runs in a locked-down sandbox that forbids all of that.
- param_schema: one entry per parameter, using EXACTLY these fields: name (snake_case, \
mm-valued lengths end in _mm), type (one of "number","integer","boolean","choice"), \
default, minimum and maximum (numbers for number/integer else null), choices (a list of \
strings for "choice" else null), description. Every dimension the user must physically \
measure to get a good fit MUST be a parameter. SET MIN/MAX GENEROUSLY: the user's measured \
value is REJECTED if it falls outside your [minimum, maximum] range, so err WIDE. Center \
the range on the size hints if given, and cover at least ~0.25x to ~4x the nominal (and \
never a range narrower than the plausible real part). Allowing a slightly-too-large value \
is fine; blocking a real measurement is not. Only keep min/max tight where geometry truly \
requires it (e.g. a wall can't be below MIN_WALL).
- overlays: for EACH critical dimension, an entry telling the UI where to draw that \
measurement on the user's photo, or null if you can't confidently locate it. Fields: \
dim_name (the exact param name), photo_index (0), kind ("dim_line" for a straight linear \
span — set points to the two [x,y] endpoints; "dim_ellipse" for a diameter of a round \
feature seen in perspective — set center [x,y], rx and ry as fractions of image WIDTH, and \
rotation in degrees; "dim_depth" for a receding measurement — set the two points). All \
coordinates are normalized [0,1] (top-left origin). Leave the fields not used by your kind \
as null.
- assumptions: short plain-English notes on anything you inferred or guessed (sizes not \
given, orientation, features you added).
- critical_dims: the list of parameter names that are FIT-CRITICAL — dimensions where being \
wrong means the part won't fit. These will require the user to physically measure them; be \
conservative and include every mating/clearance dimension.

Design for FDM printing and our DFM rules:
- FUNCTION FIRST: the part must ACTUALLY WORK for what was requested. Include the real mating/\
clearance features (a bracket needs mounting holes AND a load-bearing arm; a clip needs a \
springy jaw sized to grip; a bore must clear its shaft). Add a printing clearance (~0.2-0.4 mm) \
to any feature that must fit onto/into another object. Think about how the part is used and \
oriented before you write geometry.
- ONE CONNECTED SOLID, NO FLOATING PIECES: the automatic check REJECTS any design with more \
than one disconnected body. Every rib, boss, pointer, tab or add-on must be `.union()`-ed to \
the main body AND actually overlap it in volume (a piece merely touching at a face/edge, or \
placed near but not fused, counts as a separate floating body). Build the body, then cut holes, \
then union add-ons; return one Workplane.
- Minimum wall/feature thickness {_MIN_WALL_MM} mm (PETG). Never produce a wall thinner than this.
- The whole part must fit within a {_MAX_BBOX_MM} x {_MAX_BBOX_MM} x {_MAX_BBOX_MM} mm bounding box; \
choose parameter ranges so even the maximums stay within it.
- Prefer a flat base and self-supporting geometry (overhangs steeper than ~45 degrees from \
vertical need support — avoid them where you can). Avoid knife-edges and sub-1mm details.
- Deterministic: no randomness, no time, no I/O.

Here are two exemplar templates for STYLE and structure (the build(params) contract, reading \
params, respecting MIN_WALL). Do NOT copy them — design for the actual request:

{_EXEMPLARS}
"""

# Hand-authored in OpenAI strict-mode form (every object: additionalProperties
# false + all properties required; optionals are nullable), so no schema
# transform is needed. Anthropic uses the same object as a tool input_schema.
_GENERATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "cadquery_code": {
            "type": "string",
            "description": "A complete Python module defining build(params) -> cadquery Workplane.",
        },
        "param_schema": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["number", "integer", "boolean", "choice"],
                    },
                    "default": {"type": ["number", "string", "boolean"]},
                    "minimum": {"type": ["number", "null"]},
                    "maximum": {"type": ["number", "null"]},
                    "choices": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                    },
                    "description": {"type": "string"},
                },
                "required": [
                    "name",
                    "type",
                    "default",
                    "minimum",
                    "maximum",
                    "choices",
                    "description",
                ],
            },
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "critical_dims": {"type": "array", "items": {"type": "string"}},
        "overlays": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dim_name": {"type": "string"},
                    "photo_index": {"type": "integer"},
                    "kind": {"enum": ["dim_line", "dim_ellipse", "dim_depth"]},
                    "points": {
                        "type": ["array", "null"],
                        "items": {"type": "array", "items": {"type": "number"}},
                    },
                    "center": {"type": ["array", "null"], "items": {"type": "number"}},
                    "rx": {"type": ["number", "null"]},
                    "ry": {"type": ["number", "null"]},
                    "rotation": {"type": ["number", "null"]},
                },
                "required": [
                    "dim_name",
                    "photo_index",
                    "kind",
                    "points",
                    "center",
                    "rx",
                    "ry",
                    "rotation",
                ],
            },
        },
    },
    "required": [
        "cadquery_code",
        "param_schema",
        "assumptions",
        "critical_dims",
        "overlays",
    ],
}


class CodegenProviderError(RuntimeError):
    """The only exception type that may escape this module. Every raw SDK /
    network / parse failure is caught and re-raised as one of these with a
    human-readable cause."""


def _env_value_set(name: str) -> bool:
    value = os.getenv(name, "").strip()
    return bool(value) and not value.startswith("#")


def get_provider_name() -> str:
    return os.getenv("CODEGEN_PROVIDER", "openai").strip().lower()


def get_model_name(provider: str | None = None) -> str:
    provider = provider or get_provider_name()
    if provider not in _DEFAULT_MODELS:
        raise CodegenProviderError(
            f"Unknown CODEGEN_PROVIDER '{provider}'. Supported: {sorted(_DEFAULT_MODELS)}"
        )
    return os.getenv("CODEGEN_MODEL") or _DEFAULT_MODELS[provider]


def check_provider_configured(provider: str | None = None) -> None:
    """Fail fast (at startup) if the selected provider's key is missing."""
    provider = provider or get_provider_name()
    if provider not in _REQUIRED_ENV_VAR:
        raise CodegenProviderError(
            f"Unknown CODEGEN_PROVIDER '{provider}'. Supported: {sorted(_REQUIRED_ENV_VAR)}"
        )
    env_var = _REQUIRED_ENV_VAR[provider]
    if not _env_value_set(env_var):
        raise CodegenProviderError(
            f"CODEGEN_PROVIDER is '{provider}' but {env_var} is not set. Add "
            f"{env_var}=... to .env, or set CODEGEN_PROVIDER to a provider you have a key for."
        )


def generate_template(
    request_text: str,
    photos: list[PhotoInput],
    dims_hints: list[dict[str, Any]] | None = None,
    *,
    retry_feedback: str | None = None,
) -> dict[str, Any]:
    """Ask the model to author a one-off parametric template. Returns a dict with
    cadquery_code / param_schema / assumptions / critical_dims. Raises
    CodegenProviderError on any provider/parse failure."""
    provider = get_provider_name()
    check_provider_configured(provider)
    model = get_model_name(provider)
    user_text = _build_user_prompt(request_text, dims_hints, retry_feedback)

    if provider == "openai":
        return _generate_openai(photos, user_text, model)
    if provider == "anthropic":
        return _generate_anthropic(photos, user_text, model)
    raise CodegenProviderError(f"Unknown CODEGEN_PROVIDER '{provider}'.")


def _build_user_prompt(
    request_text: str,
    dims_hints: list[dict[str, Any]] | None,
    retry_feedback: str | None,
) -> str:
    parts = [f"User's request: {request_text}"]
    if dims_hints:
        parts.append(
            "Rough size hints inferred from the photo/text (millimetres, "
            f"uncalibrated — treat as starting points, not truth): {json.dumps(dims_hints)}"
        )
    if retry_feedback:
        parts.append(
            "Your PREVIOUS attempt failed — fix it and return a fully corrected design. "
            f"Failure detail:\n{retry_feedback}"
        )
    return "\n\n".join(parts)


def _humanize(label: str, exc: Exception) -> str:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    cls = type(exc).__name__
    try:
        text = str(exc) or cls
    except Exception:
        text = cls
    lowered = text.lower()

    def msg(reason: str) -> str:
        return f"{label} request failed: {reason} ({cls}: {text})"

    if status == 401 or "authentication" in lowered or "invalid api key" in lowered:
        return msg("authentication failed — check the API key")
    if "quota" in lowered or "billing" in lowered or "insufficient_quota" in lowered:
        return msg("quota exceeded or billing issue")
    if status == 429 or "rate limit" in lowered or "ratelimit" in cls.lower():
        return msg("rate limited — retry later")
    if status == 404 or "not_found" in lowered or "does not exist" in lowered:
        return msg("model not found — check CODEGEN_MODEL")
    if (
        "connection" in cls.lower()
        or "timeout" in cls.lower()
        or "timed out" in lowered
    ):
        return msg("could not reach the provider (network error/timeout)")
    if status is not None:
        return msg(f"provider returned HTTP {status}")
    return msg("unexpected provider error")


# ---------------------------------------------------------------------------
# OpenAI adapter
# ---------------------------------------------------------------------------


def _generate_openai(
    photos: list[PhotoInput], user_text: str, model: str
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for photo in photos:
        b64 = base64.b64encode(photo.content).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{photo.mime_type};base64,{b64}"},
            }
        )

    try:
        import openai

        client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "generated_template",
                    "schema": _GENERATION_SCHEMA,
                    "strict": True,
                },
            },
        )
    except Exception as e:
        raise CodegenProviderError(_humanize("OpenAI", e)) from e

    try:
        message = response.choices[0].message
        if not message.content:
            raise CodegenProviderError("OpenAI returned an empty message (no content).")
        return json.loads(message.content)
    except CodegenProviderError:
        raise
    except Exception as e:
        raise CodegenProviderError(
            f"Could not parse OpenAI response ({type(e).__name__}: {e})"
        ) from e


# ---------------------------------------------------------------------------
# Anthropic adapter
# ---------------------------------------------------------------------------


def _generate_anthropic(
    photos: list[PhotoInput], user_text: str, model: str
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for photo in photos:
        b64 = base64.b64encode(photo.content).decode("ascii")
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": photo.mime_type,
                    "data": b64,
                },
            }
        )
    content.append({"type": "text", "text": user_text})

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            tools=[
                {
                    "name": "record_generated_template",
                    "description": "Record the generated parametric template.",
                    "input_schema": _GENERATION_SCHEMA,
                }
            ],
            tool_choice={"type": "tool", "name": "record_generated_template"},
        )
    except Exception as e:
        raise CodegenProviderError(_humanize("Anthropic", e)) from e

    try:
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                return dict(block.input)
        raise CodegenProviderError("Anthropic returned no tool_use block.")
    except CodegenProviderError:
        raise
    except Exception as e:
        raise CodegenProviderError(
            f"Could not parse Anthropic response ({type(e).__name__}: {e})"
        ) from e
