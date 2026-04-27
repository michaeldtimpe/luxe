"""Tests for the BackendProvider factory and protocol satisfaction."""

from __future__ import annotations

import pytest

from luxe_cli.providers import (
    BackendProvider,
    LMStudioProvider,
    OllamaProvider,
    get_provider,
)


def test_get_provider_ollama_returns_ollama_instance():
    p = get_provider("ollama")
    assert isinstance(p, OllamaProvider)
    assert p.name == "ollama"
    assert p.base_url == "http://127.0.0.1:11434"


def test_get_provider_lmstudio_returns_lmstudio_instance():
    p = get_provider("lmstudio")
    assert isinstance(p, LMStudioProvider)
    assert p.name == "lmstudio"
    assert p.base_url == "http://127.0.0.1:1234"


def test_get_provider_honors_explicit_base_url():
    p = get_provider("ollama", base_url="http://other:9999")
    assert p.base_url == "http://other:9999"


def test_get_provider_unknown_kind_raises():
    with pytest.raises(ValueError, match="no provider registered"):
        get_provider("not-a-real-backend", base_url="http://x:1")


def test_providers_satisfy_protocol():
    assert isinstance(OllamaProvider(), BackendProvider)
    assert isinstance(LMStudioProvider(), BackendProvider)


def test_lmstudio_pull_stream_raises_not_implemented():
    p = LMStudioProvider()
    with pytest.raises(NotImplementedError, match="GUI"):
        next(iter(p.pull_stream("anything")))
