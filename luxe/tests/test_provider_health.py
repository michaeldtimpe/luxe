"""Tests for the multi-provider session-start health check."""

from __future__ import annotations

from luxe_cli.registry import AgentConfig, LuxeConfig, ProviderConfig
from luxe_cli.repl.core import _check_provider_health


def _agent(name, **overrides) -> AgentConfig:
    base = dict(name=name, display="x", model="m", system_prompt="p")
    base.update(overrides)
    return AgentConfig(**base)


def test_health_check_groups_by_unique_endpoint(monkeypatch):
    """Two agents on the same provider only ping once."""
    pings: list[str] = []

    def fake_ping(self) -> bool:
        pings.append(self.base_url)
        return True

    monkeypatch.setattr(
        "luxe_cli.providers.openai_compat.OpenAICompatProvider.ping", fake_ping
    )

    cfg = LuxeConfig(
        providers={"omlx": ProviderConfig(base_url="http://127.0.0.1:8000", kind="omlx")},
        default_provider="omlx",
        agents=[_agent("general"), _agent("router")],
    )
    result = _check_provider_health(cfg)
    # Both agents resolve to the same URL → exactly one ping.
    assert pings == ["http://127.0.0.1:8000"]
    assert len(result["reachable"]) == 1
    assert len(result["unreachable"]) == 0


def test_health_check_separates_reachable_from_unreachable(monkeypatch):
    """Mixed up/down providers land in the right buckets."""
    def fake_ping(self) -> bool:
        return ":8000" in self.base_url  # only oMLX is up

    monkeypatch.setattr(
        "luxe_cli.providers.openai_compat.OpenAICompatProvider.ping", fake_ping
    )
    monkeypatch.setattr(
        "luxe_cli.providers.ollama.OllamaProvider.ping", lambda self: False
    )

    cfg = LuxeConfig(
        providers={
            "omlx": ProviderConfig(base_url="http://127.0.0.1:8000", kind="omlx"),
            "ollama": ProviderConfig(base_url="http://127.0.0.1:11434", kind="ollama"),
        },
        agents=[
            _agent("general", provider="omlx"),
            _agent("research", provider="ollama"),
        ],
    )
    result = _check_provider_health(cfg)
    reach_urls = {url for url, _ in result["reachable"]}
    unreach_urls = {url for url, _ in result["unreachable"]}
    assert "http://127.0.0.1:8000" in reach_urls
    assert "http://127.0.0.1:11434" in unreach_urls


def test_health_check_skips_disabled_agents(monkeypatch):
    pings: list[str] = []

    def fake_ping(self) -> bool:
        pings.append(self.base_url)
        return True

    monkeypatch.setattr(
        "luxe_cli.providers.openai_compat.OpenAICompatProvider.ping", fake_ping
    )

    cfg = LuxeConfig(
        providers={"omlx": ProviderConfig(base_url="http://127.0.0.1:8000", kind="omlx")},
        default_provider="omlx",
        agents=[
            _agent("general", enabled=True),
            _agent("router", enabled=False, endpoint="http://other:1234"),
        ],
    )
    _check_provider_health(cfg)
    assert "http://other:1234" not in pings
