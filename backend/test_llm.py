"""Tests for provider selection in backend.llm."""

from backend import llm


def test_default_provider_prefers_openrouter(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm._provider() == "openrouter"


def test_explicit_provider_override(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert llm._provider() == "anthropic"


def test_openrouter_default_model_is_openrouter_hosted_claude(monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm._model() == "anthropic/claude-3.5-sonnet"