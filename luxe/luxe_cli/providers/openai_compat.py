"""Generic OpenAI-compat BackendProvider — base for LM Studio, oMLX, etc.

Every OpenAI-compatible local server exposes /v1/models for listing.
Some also expose /v1/models/{id} with extra metadata (LM Studio does;
oMLX returns just {id, object, created} without context length info).
We attempt the per-model lookup and degrade gracefully when the
provider doesn't return useful fields.

Concrete subclasses set `name` and the auth-env-var mapping.
"""

from __future__ import annotations

import os
from typing import Iterator

import httpx

from luxe_cli.backend import (
    _CTX_CACHE,
    _PARAMS_CACHE,
    _cache_get,
    _cache_set,
    server_process_rss_gb,
)


class OpenAICompatProvider:
    """Base for any provider that speaks the OpenAI standard.

    Default implementation handles ping/list/per-model metadata via
    /v1/models. Subclasses override `name`, `auth_env_vars`, and
    optionally any method that needs provider-specific behavior.
    """

    name: str = "openai_compat"
    auth_env_vars: tuple[str, ...] = ()

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def _auth_headers(self) -> dict[str, str]:
        for var in self.auth_env_vars:
            token = os.environ.get(var)
            if token:
                return {"Authorization": f"Bearer {token}"}
        return {}

    def ping(self) -> bool:
        try:
            r = httpx.get(
                f"{self.base_url}/v1/models",
                headers=self._auth_headers(),
                timeout=2.0,
            )
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def list_models(self) -> list[str]:
        try:
            r = httpx.get(
                f"{self.base_url}/v1/models",
                headers=self._auth_headers(),
                timeout=5.0,
            )
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        except httpx.HTTPError:
            return []

    def _model_info(self, model: str) -> dict | None:
        try:
            r = httpx.get(
                f"{self.base_url}/v1/models/{model}",
                headers=self._auth_headers(),
                timeout=5.0,
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError:
            return None

    def context_length(self, model: str) -> int | None:
        key = f"{self.base_url}::{model}"
        cached = _cache_get(_CTX_CACHE, key)
        if cached is not None:
            return cached
        info = self._model_info(model)
        if not info:
            return None
        for field in ("loaded_context_length", "max_context_length", "context_length"):
            v = info.get(field)
            if isinstance(v, int) and v > 0:
                _cache_set(_CTX_CACHE, key, v)
                return v
        return None

    def max_context_length(self, model: str) -> int | None:
        info = self._model_info(model)
        if not info:
            return None
        v = info.get("max_context_length") or info.get("context_length")
        return v if isinstance(v, int) and v > 0 else None

    def parameter_size(self, model: str) -> str | None:
        key = f"{self.base_url}::{model}"
        cached = _cache_get(_PARAMS_CACHE, key)
        if cached is not None:
            return cached
        info = self._model_info(model)
        if not info:
            return None
        for field in ("parameters", "parameter_size"):
            v = info.get(field)
            if isinstance(v, str) and v.strip():
                _cache_set(_PARAMS_CACHE, key, v)
                return v
        return None

    def estimate_kv_ram_gb(self, model: str, num_ctx: int) -> float | None:
        # /v1/models/{id} doesn't expose head_count / block_count the way
        # Ollama's /api/show does — would have to parse the GGUF directly.
        return None

    def server_process_rss_gb(self) -> float | None:
        return server_process_rss_gb(self.base_url)

    def prewarm(self, model: str) -> None:
        try:
            httpx.post(
                f"{self.base_url}/v1/chat/completions",
                headers=self._auth_headers(),
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                    "temperature": 0,
                    "stream": False,
                },
                timeout=180.0,
            )
        except httpx.HTTPError:
            pass

    def pull_stream(self, model: str) -> Iterator[dict]:
        raise NotImplementedError(
            f"{self.name}: model downloads are not exposed via the OpenAI API."
        )
