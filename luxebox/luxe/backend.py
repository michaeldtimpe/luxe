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


_CTX_CACHE: dict[str, int] = {}
_DEFAULT_CTX = 8192


def context_length(model: str, base_url: str = "http://127.0.0.1:11434") -> int:
    """Ollama's declared context window for `model`. Cached; falls back to 8192."""
    if model in _CTX_CACHE:
        return _CTX_CACHE[model]
    try:
        r = httpx.post(f"{base_url}/api/show", json={"name": model}, timeout=5.0)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError:
        _CTX_CACHE[model] = _DEFAULT_CTX
        return _DEFAULT_CTX

    ctx = _DEFAULT_CTX
    info = data.get("model_info") or {}
    for k, v in info.items():
        if k.endswith(".context_length") and isinstance(v, int):
            ctx = v
            break
    params = data.get("parameters") or ""
    for line in params.splitlines() if isinstance(params, str) else []:
        if line.strip().startswith("num_ctx"):
            try:
                ctx = int(line.split()[-1])
            except ValueError:
                pass
    _CTX_CACHE[model] = ctx
    return ctx
