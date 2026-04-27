"""Tests for LuxeConfig.resolve_endpoint precedence."""

from __future__ import annotations

import pytest

from luxe_cli.registry import AgentConfig, LuxeConfig, ProviderConfig


def _agent(**overrides) -> AgentConfig:
    base = dict(
        name="general",
        display="x",
        model="m",
        system_prompt="p",
    )
    base.update(overrides)
    return AgentConfig(**base)


def _cfg(**overrides) -> LuxeConfig:
    base = dict(
        providers={
            "ollama": ProviderConfig(base_url="http://127.0.0.1:11434", kind="ollama"),
            "omlx": ProviderConfig(base_url="http://127.0.0.1:8000", kind="omlx"),
            "lmstudio": ProviderConfig(base_url="http://127.0.0.1:1234", kind="lmstudio"),
        },
        agents=[],
    )
    base.update(overrides)
    return LuxeConfig(**base)


def test_endpoint_field_wins_over_provider():
    cfg = _cfg(default_provider="omlx")
    a = _agent(provider="lmstudio", endpoint="http://other:9999")
    assert cfg.resolve_endpoint(a) == "http://other:9999"


def test_provider_field_resolves_via_providers_map():
    cfg = _cfg()
    a = _agent(provider="lmstudio")
    assert cfg.resolve_endpoint(a) == "http://127.0.0.1:1234"


def test_default_provider_used_when_agent_unset():
    cfg = _cfg(default_provider="omlx")
    a = _agent()
    assert cfg.resolve_endpoint(a) == "http://127.0.0.1:8000"


def test_legacy_fallback_strips_v1_suffix():
    cfg = LuxeConfig(
        ollama_base_url="http://127.0.0.1:11434/v1",
        agents=[],
    )
    a = _agent()
    assert cfg.resolve_endpoint(a) == "http://127.0.0.1:11434"


def test_unknown_provider_raises():
    cfg = _cfg()
    a = _agent(provider="not-declared")
    with pytest.raises(KeyError, match="not declared"):
        cfg.resolve_endpoint(a)


def test_real_agents_yaml_resolves_each_agent():
    """End-to-end smoke: every agent in the actual config resolves to a URL."""
    from luxe_cli.registry import load_config

    cfg = load_config()
    for agent in cfg.agents:
        url = cfg.resolve_endpoint(agent)
        assert url.startswith("http"), f"{agent.name} resolved to {url!r}"
