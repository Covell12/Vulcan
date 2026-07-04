"""Tests for api/codegen_provider.py — the Track B code-generation seam.
Both SDK clients are mocked at their constructor; no real network.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from api.codegen_provider import (
    CodegenProviderError,
    PhotoInput,
    check_provider_configured,
    generate_template,
    get_model_name,
    get_provider_name,
)

GEN_OUTPUT = {
    "cadquery_code": "import cadquery as cq\ndef build(params):\n    return cq.Workplane('XY').box(10,10,10)\n",
    "param_schema": [
        {
            "name": "width_mm",
            "type": "number",
            "default": 10,
            "minimum": 5,
            "maximum": 200,
            "choices": None,
            "description": "Overall width.",
        }
    ],
    "assumptions": ["Assumed a plain cube."],
    "critical_dims": ["width_mm"],
}


def test_provider_and_model_selection(monkeypatch):
    monkeypatch.delenv("CODEGEN_PROVIDER", raising=False)
    monkeypatch.delenv("CODEGEN_MODEL", raising=False)
    assert get_provider_name() == "openai"
    assert get_model_name() == "gpt-5"
    monkeypatch.setenv("CODEGEN_PROVIDER", "anthropic")
    assert get_model_name() == "claude-opus-4-8"
    monkeypatch.setenv("CODEGEN_MODEL", "custom-model")
    assert get_model_name() == "custom-model"


def test_unknown_provider_rejected(monkeypatch):
    monkeypatch.setenv("CODEGEN_PROVIDER", "bogus")
    with pytest.raises(CodegenProviderError):
        get_model_name()


def test_check_provider_configured_fails_without_key(monkeypatch):
    monkeypatch.setenv("CODEGEN_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(CodegenProviderError):
        check_provider_configured()


def test_openai_adapter_parses_response(monkeypatch):
    monkeypatch.setenv("CODEGEN_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fake_msg = MagicMock()
    fake_msg.content = json.dumps(GEN_OUTPUT)
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(message=fake_msg)]
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_resp

    fake_openai = MagicMock()
    fake_openai.OpenAI.return_value = fake_client
    with patch.dict("sys.modules", {"openai": fake_openai}):
        out = generate_template("a widget", [PhotoInput(content=b"x")], None)
    assert out == GEN_OUTPUT


def test_anthropic_adapter_parses_response(monkeypatch):
    monkeypatch.setenv("CODEGEN_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    block = MagicMock()
    block.type = "tool_use"
    block.input = GEN_OUTPUT
    fake_resp = MagicMock()
    fake_resp.content = [block]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp

    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_client
    with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        out = generate_template("a widget", [], [{"name": "width_mm", "value_mm": 12}])
    assert out == GEN_OUTPUT


def test_provider_error_wrapped(monkeypatch):
    monkeypatch.setenv("CODEGEN_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = RuntimeError(
        "boom 429 rate limit"
    )
    fake_openai = MagicMock()
    fake_openai.OpenAI.return_value = fake_client
    with patch.dict("sys.modules", {"openai": fake_openai}):
        with pytest.raises(CodegenProviderError) as exc:
            generate_template("x", [], None)
    assert "OpenAI request failed" in str(exc.value)
