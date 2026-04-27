"""Provider protocol for backend introspection.

The Backend dataclass (harness.backends.Backend) handles the chat-
completion transport — every supported provider exposes
/v1/chat/completions, so that side is already provider-agnostic.

Introspection is not. Listing models, querying a model's context
window, checking server health, and warming a model all use
provider-specific endpoints (/api/show vs /v1/models, etc). The
BackendProvider protocol is the seam: the REPL banner, model picker,
status line, and prewarm path all consume a provider through this
interface, and #7 will land concrete Ollama and LM Studio
implementations behind it.

Methods that don't apply to a given provider (pull_stream on
LM Studio — the GUI manages downloads) should raise
NotImplementedError. Callers are expected to feature-detect via
hasattr or a try/except, not to wrap every call in a kind check.
"""

from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable

from luxe_cli.providers.lmstudio import LMStudioProvider
from luxe_cli.providers.ollama import OllamaProvider
from luxe_cli.providers.omlx import OMLXProvider
from luxe_cli.providers.openai_compat import OpenAICompatProvider


_PROVIDER_CLASSES: dict[str, type] = {
    "ollama": OllamaProvider,
    "lmstudio": LMStudioProvider,
    "omlx": OMLXProvider,
    "llamacpp": OpenAICompatProvider,  # generic OpenAI-compat fallback
    "mlx": OpenAICompatProvider,  # legacy alias for older configs
}


def get_provider(kind: str, base_url: str | None = None) -> "BackendProvider":
    """Construct the provider for a given kind. base_url defaults to the
    canonical local port for that kind (see luxe_cli.backend._BACKEND_OVERRIDE_URLS)."""
    from luxe_cli.backend import _BACKEND_OVERRIDE_URLS

    url = base_url or _BACKEND_OVERRIDE_URLS.get(kind)
    if url is None:
        raise ValueError(f"unknown provider kind: {kind!r}")
    cls = _PROVIDER_CLASSES.get(kind)
    if cls is None:
        raise ValueError(f"no provider registered for kind: {kind!r}")
    return cls(url) if cls is not OpenAICompatProvider else _generic(kind, url)


def _generic(kind: str, url: str) -> OpenAICompatProvider:
    p = OpenAICompatProvider(url)
    p.name = kind
    return p


__all__ = [
    "BackendProvider",
    "OllamaProvider",
    "LMStudioProvider",
    "OMLXProvider",
    "OpenAICompatProvider",
    "get_provider",
]


@runtime_checkable
class BackendProvider(Protocol):
    """Introspection surface that varies per provider."""

    name: str
    base_url: str

    def ping(self) -> bool:
        """Liveness check. Returns False on any HTTP error or timeout."""
        ...

    def list_models(self) -> list[str]:
        """Models the provider currently has loaded/available."""
        ...

    def context_length(self, model: str) -> int | None:
        """Configured context window for a model. None if not introspectable."""
        ...

    def max_context_length(self, model: str) -> int | None:
        """Maximum context the model architecturally supports. None if unknown."""
        ...

    def parameter_size(self, model: str) -> str | None:
        """Parameter count as a human label (e.g. "7B"). None if not reported."""
        ...

    def estimate_kv_ram_gb(self, model: str, num_ctx: int) -> float | None:
        """Estimated KV-cache RAM at the given context size. None if unknown."""
        ...

    def server_process_rss_gb(self) -> float | None:
        """Resident set size of the provider's server process. None if not local."""
        ...

    def prewarm(self, model: str) -> None:
        """Load a model into memory ahead of the first chat call."""
        ...

    def pull_stream(self, model: str) -> Iterator[dict]:
        """Stream a model download. Raise NotImplementedError if unsupported."""
        ...
