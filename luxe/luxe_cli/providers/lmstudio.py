"""LM Studio BackendProvider implementation.

LM Studio exposes the OpenAI-standard /v1/models for listing and a
per-model /v1/models/{id} for metadata (context_length, parameter
count). There is no /api/show equivalent, no pull endpoint (downloads
are managed in the GUI), and no /api/version equivalent — ping uses
/v1/models as the liveness signal.

LM_API_TOKEN / LMSTUDIO_API_KEY are auto-loaded by harness.backends.Backend
when kind="lmstudio"; introspection uses the same headers via _auth_headers.
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


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("LM_API_TOKEN") or os.environ.get("LMSTUDIO_API_KEY") or ""
    return {"Authorization": f"Bearer {token}"} if token else {}


class LMStudioProvider:
    name = "lmstudio"

    def __init__(self, base_url: str = "http://127.0.0.1:1234") -> None:
        self.base_url = base_url

    def ping(self) -> bool:
        try:
            r = httpx.get(
                f"{self.base_url}/v1/models", headers=_auth_headers(), timeout=2.0
            )
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def list_models(self) -> list[str]:
        try:
            r = httpx.get(
                f"{self.base_url}/v1/models", headers=_auth_headers(), timeout=5.0
            )
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        except httpx.HTTPError:
            return []

    def _model_info(self, model: str) -> dict | None:
        try:
            r = httpx.get(
                f"{self.base_url}/v1/models/{model}",
                headers=_auth_headers(),
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
        # LM Studio exposes loaded_context_length on currently-loaded
        # models and max_context_length always.
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
        # LM Studio's metadata varies by quant; arch.parameters is the
        # most reliable when present.
        for field in ("parameters", "parameter_size"):
            v = info.get(field)
            if isinstance(v, str) and v.strip():
                _cache_set(_PARAMS_CACHE, key, v)
                return v
        return None

    def estimate_kv_ram_gb(self, model: str, num_ctx: int) -> float | None:
        # LM Studio's /v1/models/{id} doesn't expose head_count / block_count
        # the way Ollama's /api/show does, so we can't reproduce the same
        # estimate. Returning None is the honest answer until LM Studio
        # ships the GGUF metadata or we parse it from the model file.
        return None

    def server_process_rss_gb(self) -> float | None:
        # Same psutil walk works for any local server listening on a TCP port.
        return server_process_rss_gb(self.base_url)

    def prewarm(self, model: str) -> None:
        try:
            httpx.post(
                f"{self.base_url}/v1/chat/completions",
                headers=_auth_headers(),
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
            "LM Studio downloads are managed in the GUI; no /pull endpoint."
        )
