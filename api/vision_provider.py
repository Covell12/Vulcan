"""The ONE seam for all vision-LLM access in Vulcan.

Every other module calls `parse_intent(...)` and never imports a provider
SDK or branches on a provider's name — that logic lives entirely in this
file. Switching providers is one .env edit (VISION_PROVIDER=openai|anthropic)
plus a server restart; no code changes anywhere else.

Both providers are asked to fill the SAME canonical schema
(schemas/intent_spec.schema.json), just through each provider's own
structured-output mechanism: OpenAI's strict json_schema response_format
(which requires a stricter subset of JSON Schema — see
`_to_openai_strict_schema`), and Anthropic's forced tool_use (which accepts
the schema close to as-is). Either way, the caller (api/intents.py) is the
one that actually validates the result against the canonical schema — this
module's job is just to get the model's best attempt back as a dict.
"""

from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from api.photo import PhotoInput  # re-exported; shared with api/depth_provider.py

load_dotenv()

_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "schemas" / "intent_spec.schema.json"
)

_DEFAULT_MODELS = {
    "openai": "gpt-5",
    "anthropic": "claude-opus-4-8",
}

_REQUIRED_ENV_VAR = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

SYSTEM_PROMPT = """You are the intent parser for Vulcan, a service that turns a photo of \
where a custom 3D-printed part goes, plus a short description, into a manufacturable \
part design. Your job is to look at the photo(s), any user-drawn annotation, and the \
user's text, then produce a single structured IntentSpec.

Rules:
1. Pick the best-matching template from the "available_templates" list (given to you \
in the user message as JSON) by its template_id, and set category to that template's \
category. If nothing in the catalog is a reasonable match, set category="other" and \
template_id=null. If the request is clearly out of scope (larger than roughly \
250x250x250mm, structural/load-bearing beyond a shelf bracket, automotive, medical, \
electrical, or otherwise unsafe to 3D print), set status="out_of_scope", \
category="other", template_id=null, and explain briefly in out_of_scope_reason.
2. If you picked a template, propose one dimensions[] entry for EVERY numeric \
millimeter parameter that template's "params" list describes (use the exact param \
name, e.g. "span_mm", as the dimension's "name"). Estimate each value_mm as best you \
can from the photo. Every dimension's source MUST be "assumed" — there is no depth \
sensor or calibration in this version, so nothing you propose from a photo alone can \
be source="user_measured". Give each an honest confidence (0-1): a rough visual \
estimate from an uncalibrated photo is rarely worth more than 0.3-0.5 confidence, even \
if it looks obvious. Set critical=true exactly for the param names listed as \
"critical_dims" for that template in the catalog, and critical=false for all others.
3. For EVERY dimension you marked critical=true, add one entry to questions[] asking \
the user to actually measure it (kind="measure_mm", dim_name set to that dimension's \
name, a clear one-sentence prompt e.g. "How far does the shelf need to stick out from \
the wall, in mm?"). Include an overlay on the most relevant photo: shape "arrow" or \
"circle", pointing at (or circling) roughly where on the photo that measurement should \
be taken, using normalized [x, y] coordinates in [0, 1] (x=0 is the left edge of the \
photo, y=0 is the top). Use photo_index to say which photo (0-indexed) the overlay is \
on. It's fine to also ask non-dimension clarifying questions (kind="choice" or \
"confirm") if something is genuinely ambiguous, but every critical dimension MUST get \
a question.
4. description is a one-sentence plain-English restatement of what the user wants — \
write it for a non-expert, not a spec sheet.
5. status is "needs_answers" whenever there is at least one dimension you couldn't set \
source="user_measured" for (which, per rule 2, is always true for a fresh photo \
submission) and a template was picked; "out_of_scope" per rule 1; you will basically \
never emit "ready_for_design" yourself since that requires user-confirmed \
measurements you don't have yet.
6. Output ONLY the IntentSpec structure you were given a schema/tool for. Do not add \
commentary outside it."""


class VisionProviderError(RuntimeError):
    """Raised for provider misconfiguration or an unusable provider response.

    This is the ONLY exception type that may escape this module. Every raw
    SDK / network / JSON-parse failure is caught and re-raised as one of these
    with a human-readable cause, so api/intents.py can turn it into a clean
    502 (never a bare 500). See `_humanize_provider_error`.
    """


def _env_value_set(name: str) -> bool:
    """True only if an env var holds a real value. Guards against the
    python-dotenv inline-comment landmine: `KEY=   # comment` loads the comment
    text AS the value, so a leading '#' (after stripping) counts as unset —
    otherwise a fail-fast check would be fooled into thinking a key is present."""
    value = os.getenv(name, "").strip()
    return bool(value) and not value.startswith("#")


def get_provider_name() -> str:
    return os.getenv("VISION_PROVIDER", "openai").strip().lower()


def get_model_name(provider: str | None = None) -> str:
    provider = provider or get_provider_name()
    if provider not in _DEFAULT_MODELS:
        raise VisionProviderError(
            f"Unknown VISION_PROVIDER '{provider}'. Supported: {sorted(_DEFAULT_MODELS)}"
        )
    return os.getenv("VISION_MODEL") or _DEFAULT_MODELS[provider]


def check_provider_configured(provider: str | None = None) -> None:
    """Fail fast with a clear message if the selected provider's API key is
    missing. Meant to be called once at server startup (see api/main.py's
    lifespan hook), not on every request."""
    provider = provider or get_provider_name()
    if provider not in _REQUIRED_ENV_VAR:
        raise VisionProviderError(
            f"Unknown VISION_PROVIDER '{provider}'. Supported: {sorted(_REQUIRED_ENV_VAR)}"
        )
    env_var = _REQUIRED_ENV_VAR[provider]
    if not _env_value_set(env_var):
        raise VisionProviderError(
            f"VISION_PROVIDER is '{provider}' but {env_var} is not set. Add "
            f"{env_var}=... to .env (see .env.example), or set VISION_PROVIDER to a "
            "provider you have a key for."
        )


def parse_intent(
    photos: list[PhotoInput],
    annotation: list[dict[str, Any]] | None,
    text: str,
    template_catalog: list[dict[str, Any]],
    *,
    retry_feedback: str | None = None,
) -> dict[str, Any]:
    """The one interface every other module calls. Returns a dict shaped like
    schemas/intent_spec.schema.json — the caller is responsible for actually
    validating it against that schema (see api/intents.py)."""
    provider = get_provider_name()
    check_provider_configured(provider)
    model = get_model_name(provider)
    user_text = _build_user_prompt(annotation, text, template_catalog, retry_feedback)

    if provider == "openai":
        return _parse_intent_openai(photos, user_text, model)
    if provider == "anthropic":
        return _parse_intent_anthropic(photos, user_text, model)
    raise VisionProviderError(f"Unknown VISION_PROVIDER '{provider}'.")


def _build_user_prompt(
    annotation: list[dict[str, Any]] | None,
    text: str,
    template_catalog: list[dict[str, Any]],
    retry_feedback: str | None,
) -> str:
    parts = [f"User's request: {text}"]

    if annotation:
        parts.append(
            "The user circled/traced these normalized [x,y] regions on the photo(s) "
            f"to show where the part goes: {json.dumps(annotation)}"
        )
    else:
        parts.append("The user did not draw any annotation on the photo(s).")

    parts.append(f"available_templates = {json.dumps(template_catalog, indent=2)}")

    if retry_feedback:
        parts.append(
            "Your previous answer failed schema validation with this error — fix it "
            f"and try again, returning a fully corrected result: {retry_feedback}"
        )

    return "\n\n".join(parts)


def _load_canonical_schema() -> dict[str, Any]:
    with open(_SCHEMA_PATH) as f:
        return json.load(f)


def _humanize_provider_error(label: str, exc: Exception) -> str:
    """Turn a raw provider SDK / network exception into a message a human can
    act on, without importing any provider's error classes (so this stays
    provider-agnostic and can't drift when SDKs reshuffle their exceptions).

    We sniff the two things provider SDKs reliably expose: an HTTP-ish
    `status_code` attribute and the string form of the exception.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    cls = type(exc).__name__
    # str(exc) can itself raise for a pathological exception — never let that
    # escape (it would defeat the whole point of this wrapper).
    try:
        text = str(exc) or cls
    except Exception:
        text = cls
    lowered = text.lower()

    def msg(reason: str) -> str:
        return f"{label} request failed: {reason} ({cls}: {text})"

    if (
        status == 401
        or "authenticationerror" in cls.lower()
        or "invalid api key" in lowered
    ):
        return msg("authentication failed — check the API key")
    if "insufficient_quota" in lowered or "quota" in lowered or "billing" in lowered:
        return msg("quota exceeded or billing issue on the provider account")
    if status == 429 or "ratelimit" in cls.lower() or "rate limit" in lowered:
        return msg("rate limited — too many requests, retry later")
    if (
        status == 404
        or "notfound" in cls.lower()
        or "model_not_found" in lowered
        or "does not exist" in lowered
    ):
        return msg("model not found — check VISION_MODEL / the default model id")
    if status == 400 or "badrequest" in cls.lower() or "invalid_request" in lowered:
        return msg("bad request — possibly an invalid or unsupported image")
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


def _parse_intent_openai(
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

    # Any failure — including a missing/broken SDK install — constructing the
    # client or making the call (auth, quota, rate limit, bad model, bad image,
    # network) becomes a VisionProviderError. The import is inside the try so an
    # ImportError doesn't leak raw.
    try:
        import openai

        client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "intent_spec",
                    "schema": _to_openai_strict_schema(_load_canonical_schema()),
                    "strict": True,
                },
            },
        )
    except Exception as e:
        raise VisionProviderError(_humanize_provider_error("OpenAI", e)) from e

    # Parsing the response is just as failure-prone (empty content, unexpected
    # shape, non-JSON body) and must not leak a raw exception either.
    try:
        message = response.choices[0].message
        if not message.content:
            raise VisionProviderError("OpenAI returned an empty message (no content).")
        return json.loads(message.content)
    except VisionProviderError:
        raise
    except Exception as e:
        raise VisionProviderError(
            f"Could not parse OpenAI response ({type(e).__name__}: {e})"
        ) from e


def _to_openai_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI's strict structured-output mode requires every object to set
    additionalProperties=false and list ALL of its properties in `required`
    (optional properties become nullable instead of omittable). Derived
    programmatically from the canonical schema so the two can't drift apart."""
    schema = copy.deepcopy(schema)
    schema.pop("$schema", None)
    schema.pop("$id", None)
    _strictify(schema)
    return schema


def _strictify(node: Any) -> None:
    if isinstance(node, dict):
        node.pop("default", None)
        if _is_object_schema(node) and "properties" in node:
            required = set(node.get("required", []))
            for prop_name, prop_schema in node["properties"].items():
                if prop_name not in required:
                    _make_nullable(prop_schema)
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        for value in node.values():
            _strictify(value)
    elif isinstance(node, list):
        for item in node:
            _strictify(item)


def _make_nullable(prop_schema: dict[str, Any]) -> None:
    if "type" in prop_schema:
        t = prop_schema["type"]
        if isinstance(t, list):
            if "null" not in t:
                t.append("null")
        elif t != "null":
            prop_schema["type"] = [t, "null"]
    elif "enum" in prop_schema and None not in prop_schema["enum"]:
        prop_schema["enum"] = [*prop_schema["enum"], None]


def _is_object_schema(node: dict[str, Any]) -> bool:
    t = node.get("type")
    return t == "object" or (isinstance(t, list) and "object" in t)


# ---------------------------------------------------------------------------
# Anthropic adapter
# ---------------------------------------------------------------------------


def _parse_intent_anthropic(
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

    # Import inside the try so a missing/broken SDK becomes VisionProviderError.
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            tools=[
                {
                    "name": "record_intent_spec",
                    "description": "Record the structured IntentSpec for this request.",
                    "input_schema": _to_anthropic_tool_schema(_load_canonical_schema()),
                }
            ],
            tool_choice={"type": "tool", "name": "record_intent_spec"},
        )
    except Exception as e:
        raise VisionProviderError(_humanize_provider_error("Anthropic", e)) from e

    try:
        for block in response.content:
            if block.type == "tool_use":
                return block.input
    except VisionProviderError:
        raise
    except Exception as e:
        raise VisionProviderError(
            f"Could not parse Anthropic response ({type(e).__name__}: {e})"
        ) from e
    raise VisionProviderError("Anthropic response did not include a tool_use block.")


def _to_anthropic_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Anthropic tool input_schema accepts plain JSON Schema — just strip the
    top-level metadata keys that describe the *document*, not the shape."""
    schema = copy.deepcopy(schema)
    for key in ("$schema", "$id", "title"):
        schema.pop(key, None)
    return schema
