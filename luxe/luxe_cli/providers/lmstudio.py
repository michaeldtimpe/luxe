"""LM Studio BackendProvider — thin OpenAICompatProvider subclass.

LM Studio's standard /v1/models endpoint returns minimal data
({id, object, owned_by}). The rich metadata (max_context_length,
arch, quantization, state) lives at the proprietary /api/v0/models
and /api/v0/models/{id}. We prefer v0 for introspection but keep
chat traffic on the OpenAI-standard /v1/chat/completions.

Auth is OFF by default in the local server; env vars LM_API_TOKEN /
LMSTUDIO_API_KEY are honored when set.
"""

from __future__ import annotations

from typing import Iterator

import httpx

from luxe_cli.backend import _CTX_CACHE, _PARAMS_CACHE, _cache_get, _cache_set
from luxe_cli.providers.openai_compat import OpenAICompatProvider


class LMStudioProvider(OpenAICompatProvider):
    name = "lmstudio"
    auth_env_vars = ("LM_API_TOKEN", "LMSTUDIO_API_KEY")

    def __init__(self, base_url: str = "http://127.0.0.1:1234") -> None:
        super().__init__(base_url)

    def _model_info(self, model: str) -> dict | None:
        # /api/v0/models/{id} carries max_context_length, arch, quantization.
        try:
            r = httpx.get(
                f"{self.base_url}/api/v0/models/{model}",
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
        for field in ("loaded_context_length", "max_context_length"):
            v = info.get(field)
            if isinstance(v, int) and v > 0:
                _cache_set(_CTX_CACHE, key, v)
                return v
        return None

    def parameter_size(self, model: str) -> str | None:
        key = f"{self.base_url}::{model}"
        cached = _cache_get(_PARAMS_CACHE, key)
        if cached is not None:
            return cached
        info = self._model_info(model)
        if not info:
            return None
        # /api/v0/models/{id} doesn't report a parameter count directly,
        # but the id usually contains it (e.g. "gemma-3-27b-it-qat" → 27B).
        # Match a digit followed by 'b' anywhere in the id.
        import re

        m = re.search(r"(\d+(?:\.\d+)?)b\b", model.lower())
        if m:
            result = f"{m.group(1)}B"
            _cache_set(_PARAMS_CACHE, key, result)
            return result
        return None

    def pull_stream(self, model: str) -> Iterator[dict]:
        raise NotImplementedError(
            "LM Studio downloads are managed in the GUI; no /pull endpoint."
        )
