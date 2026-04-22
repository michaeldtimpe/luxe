"""Thin wrapper around harness.backends.Backend targeted at Ollama.

Ollama exposes an OpenAI-compatible endpoint at /v1/chat/completions, so the
existing Backend class works unchanged. We just provide a factory.
"""

from __future__ import annotations

import httpx

from harness.backends import Backend


def make_backend(model: str, base_url: str = "http://127.0.0.1:11434") -> Backend:
    # harness.backends.Backend posts to "/v1/chat/completions", so the
    # base_url must NOT include the /v1 suffix itself.
    return Backend(kind="mlx", base_url=base_url, model_id=model, timeout_s=600.0)


def ping(base_url: str = "http://127.0.0.1:11434") -> bool:
    try:
        r = httpx.get(f"{base_url}/api/version", timeout=2.0)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def list_models(base_url: str = "http://127.0.0.1:11434") -> list[str]:
    try:
        r = httpx.get(f"{base_url}/api/tags", timeout=5.0)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except httpx.HTTPError:
        return []
