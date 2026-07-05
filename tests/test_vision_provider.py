"""Tests for api/vision_provider.py: provider/model selection from env vars,
the fail-fast startup check, the OpenAI strict-schema transform, and the
two-adapter conformance test (same mocked upstream response through both
adapters must produce identical output). No real network calls — both SDK
clients are mocked at their constructor.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from api.vision_provider import (
    PhotoInput,
    VisionProviderError,
    _is_object_schema,
    _load_canonical_schema,
    _to_anthropic_tool_schema,
    _to_openai_strict_schema,
    check_provider_configured,
    env_shadowing,
    get_model_name,
    get_provider_name,
    parse_intent,
)

SAMPLE_INTENT = {
    "intent_id": "abc",
    "status": "needs_answers",
    "category": "bracket",
    "template_id": "bracket_shelf_l",
    "description": "A shelf bracket.",
    "context_notes": "",
    "material_suggestion": "PETG",
    "out_of_scope_reason": None,
    "dimensions": [],
    "questions": [],
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in (
        "VISION_PROVIDER",
        "VISION_MODEL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Provider/model selection
# ---------------------------------------------------------------------------


def test_default_provider_is_openai():
    assert get_provider_name() == "openai"


def test_provider_overridden_by_env(monkeypatch):
    monkeypatch.setenv("VISION_PROVIDER", "anthropic")
    assert get_provider_name() == "anthropic"


def test_provider_name_is_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.setenv("VISION_PROVIDER", "  Anthropic  ")
    assert get_provider_name() == "anthropic"


def test_default_models():
    assert get_model_name("openai") == "gpt-5"
    assert get_model_name("anthropic") == "claude-opus-4-8"


def test_model_overridden_by_env(monkeypatch):
    monkeypatch.setenv("VISION_MODEL", "custom-model")
    assert get_model_name("openai") == "custom-model"


def test_unknown_provider_raises():
    with pytest.raises(VisionProviderError):
        get_model_name("bogus")


# ---------------------------------------------------------------------------
# Fail-fast startup check
# ---------------------------------------------------------------------------


def test_check_provider_configured_raises_when_key_missing(monkeypatch):
    monkeypatch.setenv("VISION_PROVIDER", "openai")
    with pytest.raises(VisionProviderError, match="OPENAI_API_KEY"):
        check_provider_configured()


def test_check_provider_configured_passes_when_key_present(monkeypatch):
    monkeypatch.setenv("VISION_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    check_provider_configured()  # must not raise


def test_check_provider_configured_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("VISION_PROVIDER", "bogus")
    with pytest.raises(VisionProviderError):
        check_provider_configured()


# ---------------------------------------------------------------------------
# OpenAI strict-schema transform
# ---------------------------------------------------------------------------


def test_openai_strict_schema_every_object_is_fully_compliant():
    """OpenAI's strict structured-output mode requires every object node
    (however deeply nested, however it got there) to set
    additionalProperties=false and list every one of its own properties in
    `required`. Walk the whole transformed tree and check this holds
    everywhere, not just at the top level."""
    strict = _to_openai_strict_schema(_load_canonical_schema())

    def check(node, path="root"):
        if isinstance(node, dict):
            if _is_object_schema(node) and "properties" in node:
                assert (
                    node.get("additionalProperties") is False
                ), f"{path}: missing additionalProperties=false"
                required = set(node.get("required", []))
                props = set(node["properties"].keys())
                assert (
                    required == props
                ), f"{path}: required {required} != properties {props}"
            for key, value in node.items():
                check(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                check(item, f"{path}[{i}]")

    check(strict)


def test_openai_strict_schema_is_json_serializable():
    # Guards against stray non-serializable values (e.g. Python `None` used
    # somewhere json.dumps can't reach) making it into the API payload.
    json.dumps(_to_openai_strict_schema(_load_canonical_schema()))


def test_anthropic_tool_schema_strips_document_metadata():
    tool_schema = _to_anthropic_tool_schema(_load_canonical_schema())
    assert "$schema" not in tool_schema
    assert "$id" not in tool_schema
    assert "title" not in tool_schema
    assert tool_schema["type"] == "object"
    assert "dimensions" in tool_schema["properties"]


# ---------------------------------------------------------------------------
# Adapters (mocked SDK clients — no network)
# ---------------------------------------------------------------------------


def _fake_openai_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=json.dumps(payload)))]
    return response


def _fake_anthropic_response(payload: dict) -> MagicMock:
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = payload
    response = MagicMock()
    response.content = [tool_block]
    return response


def test_openai_adapter_returns_parsed_payload(monkeypatch):
    monkeypatch.setenv("VISION_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    with patch("openai.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = (
            _fake_openai_response(SAMPLE_INTENT)
        )
        result = parse_intent([PhotoInput(b"fake")], None, "a bracket please", [])

    assert result == SAMPLE_INTENT
    call_kwargs = MockOpenAI.return_value.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "gpt-5"
    assert call_kwargs["response_format"]["json_schema"]["strict"] is True


def test_anthropic_adapter_returns_parsed_payload(monkeypatch):
    monkeypatch.setenv("VISION_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    with patch("anthropic.Anthropic") as MockAnthropic:
        MockAnthropic.return_value.messages.create.return_value = (
            _fake_anthropic_response(SAMPLE_INTENT)
        )
        result = parse_intent([PhotoInput(b"fake")], None, "a bracket please", [])

    assert result == SAMPLE_INTENT
    call_kwargs = MockAnthropic.return_value.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-opus-4-8"
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "record_intent_spec"}


def test_anthropic_adapter_raises_if_no_tool_use_block(monkeypatch):
    monkeypatch.setenv("VISION_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    text_block = MagicMock()
    text_block.type = "text"
    response = MagicMock()
    response.content = [text_block]

    with patch("anthropic.Anthropic") as MockAnthropic:
        MockAnthropic.return_value.messages.create.return_value = response
        with pytest.raises(VisionProviderError):
            parse_intent([PhotoInput(b"fake")], None, "a bracket please", [])


def test_parse_intent_raises_before_any_network_call_if_key_missing(monkeypatch):
    monkeypatch.setenv("VISION_PROVIDER", "openai")
    with patch("openai.OpenAI") as MockOpenAI:
        with pytest.raises(VisionProviderError):
            parse_intent([PhotoInput(b"fake")], None, "text", [])
    MockOpenAI.assert_not_called()


# ---------------------------------------------------------------------------
# SDK / network / parse failures must ALL become VisionProviderError with a
# human-readable cause — never a bare exception (so api/intents.py can 502).
# ---------------------------------------------------------------------------


def _openai_exc(status=None, message="boom"):
    exc = Exception(message)
    if status is not None:
        exc.status_code = status
    return exc


@pytest.mark.parametrize(
    "exc, expect",
    [
        (_openai_exc(401, "Invalid API key"), "authentication failed"),
        (_openai_exc(429, "Too many requests"), "rate limited"),
        (_openai_exc(404, "The model does not exist"), "model not found"),
        (_openai_exc(400, "invalid image data"), "bad request"),
        (_openai_exc(message="You exceeded your quota (insufficient_quota)"), "quota"),
        (_openai_exc(message="totally unexpected"), "unexpected provider error"),
    ],
)
def test_openai_sdk_exception_becomes_vision_error(monkeypatch, exc, expect):
    monkeypatch.setenv("VISION_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with patch("openai.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.side_effect = exc
        with pytest.raises(VisionProviderError) as ei:
            parse_intent([PhotoInput(b"x")], None, "hi", [])
    assert expect in str(ei.value)


def test_openai_bad_json_becomes_vision_error(monkeypatch):
    monkeypatch.setenv("VISION_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content="not json{"))]
    with patch("openai.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = resp
        with pytest.raises(VisionProviderError, match="parse OpenAI"):
            parse_intent([PhotoInput(b"x")], None, "hi", [])


def test_openai_empty_content_becomes_vision_error(monkeypatch):
    monkeypatch.setenv("VISION_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=None))]
    with patch("openai.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = resp
        with pytest.raises(VisionProviderError, match="empty message"):
            parse_intent([PhotoInput(b"x")], None, "hi", [])


def test_anthropic_sdk_exception_becomes_vision_error(monkeypatch):
    monkeypatch.setenv("VISION_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    exc = Exception("service unavailable")
    exc.status_code = 429
    with patch("anthropic.Anthropic") as MockAnthropic:
        MockAnthropic.return_value.messages.create.side_effect = exc
        with pytest.raises(VisionProviderError, match="rate limited"):
            parse_intent([PhotoInput(b"x")], None, "hi", [])


def test_openai_import_error_becomes_vision_error(monkeypatch):
    """Regression (review finding #3): a missing/broken SDK install must surface
    as VisionProviderError, not a raw ImportError."""
    import sys

    monkeypatch.setenv("VISION_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with patch.dict(sys.modules, {"openai": None}):  # `import openai` -> ImportError
        with pytest.raises(VisionProviderError):
            parse_intent([PhotoInput(b"x")], None, "hi", [])


# ---------------------------------------------------------------------------
# The startup check must not be fooled by a python-dotenv inline-comment value.
# ---------------------------------------------------------------------------


def test_check_ignores_inline_comment_value(monkeypatch):
    """Regression (review finding #5): `KEY=   # comment` loads '# comment' as the
    value; that must count as unset so the fail-fast still fires."""
    monkeypatch.setenv("VISION_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "# needed from Milestone 3")
    with pytest.raises(VisionProviderError, match="OPENAI_API_KEY"):
        check_provider_configured()


# ---------------------------------------------------------------------------
# Conformance: both adapters must produce IDENTICAL output for the same
# underlying model answer, just unwrapped from each provider's own envelope.
# ---------------------------------------------------------------------------


def test_conformance_openai_and_anthropic_produce_identical_output(monkeypatch):
    payload = {
        **SAMPLE_INTENT,
        "dimensions": [
            {
                "name": "span_mm",
                "value_mm": 150.0,
                "source": "assumed",
                "confidence": 0.4,
                "critical": True,
                "cross_check": None,
            }
        ],
        "questions": [
            {
                "question_id": "q1",
                "dim_name": "span_mm",
                "prompt": "How far?",
                "kind": "measure_mm",
                "choices": None,
                "overlay": {
                    "photo_index": 0,
                    "shape": "arrow",
                    "points": [[0.1, 0.2], [0.3, 0.4]],
                },
            }
        ],
    }
    photos = [PhotoInput(b"fake-bytes", "image/jpeg")]

    monkeypatch.setenv("VISION_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with patch("openai.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = (
            _fake_openai_response(payload)
        )
        openai_result = parse_intent(photos, None, "a bracket please", [])

    monkeypatch.setenv("VISION_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with patch("anthropic.Anthropic") as MockAnthropic:
        MockAnthropic.return_value.messages.create.return_value = (
            _fake_anthropic_response(payload)
        )
        anthropic_result = parse_intent(photos, None, "a bracket please", [])

    assert openai_result == anthropic_result == payload
    assert set(openai_result.keys()) == set(anthropic_result.keys())


# ---------------------------------------------------------------------------
# Provider-isolation enforcement: no other module may import a provider SDK.
# ---------------------------------------------------------------------------


def test_no_other_module_imports_provider_sdks():
    import pathlib
    import re

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    pattern = re.compile(r"^\s*(import (openai|anthropic)\b|from (openai|anthropic)\b)")
    # Both LLM-provider seams legitimately import these SDKs: the vision parser
    # and (Track B) the freeform code generator. Nothing else may.
    allowed = {"vision_provider.py", "codegen_provider.py", "test_vision_provider.py"}
    offenders = []

    for path in repo_root.rglob("*.py"):
        if ".venv" in path.parts or path.name in allowed:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.match(line):
                offenders.append(
                    f"{path.relative_to(repo_root)}:{lineno}: {line.strip()}"
                )

    assert not offenders, (
        "only api/vision_provider.py and api/codegen_provider.py may import a "
        "provider SDK:\n" + "\n".join(offenders)
    )


# --- Shell-env-shadows-.env detection (the "why is it still anthropic?" trap) ---


def _write_env(tmp_path, body: str):
    p = tmp_path / ".env"
    p.write_text(body)
    return p


def test_env_shadowing_detects_shell_override(tmp_path, monkeypatch):
    """A shell VISION_PROVIDER that differs from .env is reported as shadowing."""
    env = _write_env(tmp_path, "VISION_PROVIDER=openai\n")
    monkeypatch.setenv("VISION_PROVIDER", "anthropic")
    assert env_shadowing("VISION_PROVIDER", env) == ("anthropic", "openai")


def test_env_shadowing_none_when_values_agree(tmp_path, monkeypatch):
    env = _write_env(tmp_path, "VISION_PROVIDER=openai\n")
    monkeypatch.setenv("VISION_PROVIDER", "openai")
    assert env_shadowing("VISION_PROVIDER", env) is None


def test_env_shadowing_none_when_no_shell_var(tmp_path, monkeypatch):
    env = _write_env(tmp_path, "VISION_PROVIDER=openai\n")
    monkeypatch.delenv("VISION_PROVIDER", raising=False)
    assert env_shadowing("VISION_PROVIDER", env) is None


def test_env_shadowing_none_when_key_absent_from_dotenv(tmp_path, monkeypatch):
    env = _write_env(tmp_path, "OPENAI_API_KEY=sk-x\n")  # no VISION_PROVIDER line
    monkeypatch.setenv("VISION_PROVIDER", "anthropic")
    assert env_shadowing("VISION_PROVIDER", env) is None


def test_env_shadowing_ignores_case_and_whitespace(tmp_path, monkeypatch):
    env = _write_env(tmp_path, "VISION_PROVIDER=OpenAI\n")
    monkeypatch.setenv("VISION_PROVIDER", "  openai  ")
    assert env_shadowing("VISION_PROVIDER", env) is None


def test_dotenv_override_makes_env_win_over_shell(tmp_path, monkeypatch):
    """The fix that stops a stale shell var from shadowing .env: loading .env with
    override=True (as api/vision_provider.py now does) makes the FILE value win
    over an already-set OS/shell variable."""
    from dotenv import load_dotenv

    env = _write_env(tmp_path, "VISION_PROVIDER=openai\n")
    monkeypatch.setenv("VISION_PROVIDER", "anthropic")  # a stale shell export
    load_dotenv(env, override=True)
    assert get_provider_name() == "openai"  # .env won, shell ignored
