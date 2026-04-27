"""Tests for the BackendProvider factory and protocol satisfaction."""

from __future__ import annotations

import pytest

from luxe_cli.providers import (
    BackendProvider,
    LMStudioProvider,
    OllamaProvider,
    OMLXProvider,
    OpenAICompatProvider,
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


def test_get_provider_omlx_returns_omlx_instance():
    p = get_provider("omlx")
    assert isinstance(p, OMLXProvider)
    assert p.name == "omlx"
    assert p.base_url == "http://127.0.0.1:8000"


def test_get_provider_llamacpp_returns_generic_openai_compat():
    p = get_provider("llamacpp")
    assert isinstance(p, OpenAICompatProvider)
    assert p.name == "llamacpp"
    assert p.base_url == "http://127.0.0.1:8088"


def test_default_provider_uses_yaml_default():
    from luxe_cli.registry import load_config
    from luxe_cli.repl.models import _default_provider

    cfg = load_config()
    p = _default_provider(cfg)
    assert p.name == cfg.default_provider
