"""Ollama BackendProvider implementation.

Wraps the module-level functions in luxe_cli.backend so the introspection
surface is reachable through the BackendProvider protocol without
duplicating logic. The existing module-level functions stay in place
for now — consumer migration to provider-instance calls happens in #9.
"""

from __future__ import annotations

from typing import Iterator

from luxe_cli import backend as _backend


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str = "http://127.0.0.1:11434") -> None:
        self.base_url = base_url

    def ping(self) -> bool:
        return _backend.ping(self.base_url)

    def list_models(self) -> list[str]:
        return _backend.list_models(self.base_url)

    def context_length(self, model: str) -> int | None:
        return _backend.context_length(model, self.base_url)

    def max_context_length(self, model: str) -> int | None:
        return _backend.max_context_length(model, self.base_url)

    def parameter_size(self, model: str) -> str | None:
        ps = _backend.parameter_size(model, self.base_url)
        return ps if ps and ps != "—" else None

    def estimate_kv_ram_gb(self, model: str, num_ctx: int) -> float | None:
        return _backend.estimate_kv_ram_gb(model, num_ctx, self.base_url)

    def server_process_rss_gb(self) -> float | None:
        return _backend.server_process_rss_gb(self.base_url)

    def prewarm(self, model: str) -> None:
        _backend.prewarm(model, self.base_url)

    def pull_stream(self, model: str) -> Iterator[dict]:
        return _backend.pull_stream(model, self.base_url)
