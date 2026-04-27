"""LM Studio integration smoke test.

Two layers:

1. Static (always runs): a synthetic agents.yaml with one
   provider:lmstudio agent dispatches to a Backend with
   kind="lmstudio" and the LM Studio URL, AND session events get
   tagged with the right provider name.

2. Live (skipped if LM Studio isn't running): hits /api/v0/models
   for context length + parameter size on real loaded weights.

The live half is what you'd run before flipping a real agent for
the migration. The static half catches dispatch regressions in CI.
"""

from __future__ import annotations

import httpx
import pytest

from luxe_cli.backend import make_backend
from luxe_cli.providers import LMStudioProvider
from luxe_cli.registry import AgentConfig, LuxeConfig, ProviderConfig
from luxe_cli.session import Session


def _has_lmstudio() -> bool:
    try:
        return (
            httpx.get("http://127.0.0.1:1234/v1/models", timeout=1.0).status_code
            == 200
        )
    except httpx.HTTPError:
        return False


def test_agent_with_provider_lmstudio_dispatches_correctly():
    cfg = LuxeConfig(
        providers={
            "omlx": ProviderConfig(base_url="http://127.0.0.1:8000", kind="omlx"),
            "lmstudio": ProviderConfig(
                base_url="http://127.0.0.1:1234", kind="lmstudio"
            ),
        },
        default_provider="omlx",
        agents=[
            AgentConfig(
                name="lookup",
                display="x",
                model="qwen2.5-32b-instruct",
                system_prompt="p",
                provider="lmstudio",
            ),
        ],
    )
    agent = cfg.get("lookup")
    endpoint = cfg.resolve_endpoint(agent)
    assert endpoint == "http://127.0.0.1:1234"

    backend = make_backend(agent.model, base_url=endpoint)
    assert backend.kind == "lmstudio"
    assert backend.base_url == "http://127.0.0.1:1234"
    assert backend.model_id == "qwen2.5-32b-instruct"


def test_lmstudio_dispatch_tags_session_events(tmp_path):
    """Confirms #12's binding wiring covers an LM Studio dispatch path."""
    sess = Session.new(tmp_path, first_prompt="t")
    backend = make_backend("any-model", base_url="http://127.0.0.1:1234")
    with sess.bind_backend(backend.kind, backend.base_url):
        sess.append({"role": "user", "agent": "lookup", "content": "hello"})
    events = sess.read_all()
    assert events[0]["provider"] == "lmstudio"
    assert events[0]["base_url"] == "http://127.0.0.1:1234"


@pytest.mark.skipif(not _has_lmstudio(), reason="LM Studio not running on :1234")
def test_lmstudio_provider_lists_models_live():
    """Live: requires LM Studio. Skipped in CI."""
    p = LMStudioProvider()
    assert p.ping()
    models = p.list_models()
    assert isinstance(models, list)
    assert len(models) > 0


@pytest.mark.skipif(not _has_lmstudio(), reason="LM Studio not running on :1234")
def test_lmstudio_provider_returns_context_length_live():
    """Live: at least one model should report a context length."""
    p = LMStudioProvider()
    models = p.list_models()
    ctx_lengths = [p.context_length(m) for m in models]
    assert any(c is not None and c > 0 for c in ctx_lengths), \
        "no LM Studio model reported a context length via /api/v0/models/{id}"
