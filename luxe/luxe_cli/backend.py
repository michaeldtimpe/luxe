"""Thin wrapper around harness.backends.Backend targeted at Ollama.

Ollama exposes an OpenAI-compatible endpoint at /v1/chat/completions, so the
existing Backend class works unchanged. We just provide a factory.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Iterator

import httpx

from harness.backends import Backend


_CACHE_TTL_S = float(os.environ.get("LUXE_CACHE_TTL_S", "300"))


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


def _cache_get(cache: dict[str, "_CacheEntry"], key: str) -> Any | None:
    entry = cache.get(key)
    if entry is None:
        return None
    if time.monotonic() > entry.expires_at:
        cache.pop(key, None)
        return None
    return entry.value


def _cache_set(cache: dict[str, "_CacheEntry"], key: str, value: Any) -> None:
    cache[key] = _CacheEntry(value=value, expires_at=time.monotonic() + _CACHE_TTL_S)

# Curated map of Ollama model-family → released parameter sizes. This is
# maintained by hand — there is no public Ollama API that enumerates every
# tag for a family. Add entries as new families land locally.
MODEL_VARIANTS: dict[str, list[str]] = {
    "gemma3": ["1b", "4b", "12b", "27b"],
    "gemma2": ["2b", "9b", "27b"],
    "qwen2.5": ["0.5b", "1.5b", "3b", "7b", "14b", "32b", "72b"],
    "qwen2.5-coder": ["0.5b", "1.5b", "3b", "7b", "14b", "32b"],
    "qwen2.5vl": ["3b", "7b", "32b", "72b"],
    "llama3.3": ["70b"],
    "llama3.2": ["1b", "3b"],
    "llama3.2-vision": ["11b", "90b"],
    "llama3.1": ["8b", "70b", "405b"],
    "llava": ["7b", "13b", "34b"],
    "minicpm-v": ["8b"],
    "mistral-small": ["22b", "24b"],
    "mistral": ["7b"],
    "mistral-nemo": ["12b"],
    "command-r": ["35b"],
    "command-r-plus": ["104b"],
    "mixtral": ["8x7b", "8x22b"],
    "phi3.5": ["3.8b"],
    "phi4": ["14b"],
    "nomic-embed-text": ["137m"],
    "deepseek-r1": ["1.5b", "7b", "8b", "14b", "32b", "70b", "671b"],
    "deepseek-v3": ["671b"],
}


_VARIANT_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?x?\d*b|\d+m)", re.IGNORECASE)


def family_of(tag: str) -> str:
    """`qwen2.5:7b-instruct` → `qwen2.5`."""
    return tag.split(":", 1)[0]


def size_of(tag: str) -> str:
    """Best-effort extract of param size from an Ollama tag's variant part.
    `qwen2.5:7b-instruct` → `7b`; `mixtral:8x7b-…` → `8x7b`; no match → ``."""
    variant = tag.split(":", 1)[1] if ":" in tag else ""
    m = _VARIANT_SIZE_RE.search(variant)
    return m.group(1).lower() if m else ""


def installed_by_family(models: list[str]) -> dict[str, set[str]]:
    """Group installed Ollama tags by family, collapsing variants to size."""
    out: dict[str, set[str]] = {}
    for tag in models:
        sz = size_of(tag)
        out.setdefault(family_of(tag), set()).add(sz or tag.split(":", 1)[-1])
    return out


# Known endpoints for the supported externally-managed backends. Used
# by LUXE_BACKEND_OVERRIDE so a Phase 4 orchestrator run can be
# redirected at oMLX or llama-server without editing agents.yaml.
# Override either by setting LUXE_BACKEND_OVERRIDE=<kind>, or by
# pointing LUXE_BACKEND_OVERRIDE_URL=http://127.0.0.1:<port> directly.
_BACKEND_OVERRIDE_URLS: dict[str, str] = {
    "ollama": "http://127.0.0.1:11434",
    "llamacpp": "http://127.0.0.1:8088",
    "omlx": "http://127.0.0.1:8000",
}


def _resolve_override(default_base_url: str) -> str:
    """Honor LUXE_BACKEND_OVERRIDE_URL (full URL) first, then
    LUXE_BACKEND_OVERRIDE (named kind). Unknown kind names fall through
    to the caller-supplied default rather than raising — the caller's
    invocation should still work even with a malformed env var."""
    explicit = os.environ.get("LUXE_BACKEND_OVERRIDE_URL", "").strip()
    if explicit:
        return explicit
    kind = os.environ.get("LUXE_BACKEND_OVERRIDE", "").strip().lower()
    if kind and kind in _BACKEND_OVERRIDE_URLS:
        return _BACKEND_OVERRIDE_URLS[kind]
    return default_base_url


def _resolve_model_override(default_model: str) -> str:
    """Honor LUXE_MODEL_OVERRIDE so an orchestrator run can redirect
    not just the URL but the model name as well — necessary when the
    same logical candidate is served under different tags by different
    backends (e.g. oMLX `Qwen2.5-32B-Instruct-4bit` vs Ollama
    `qwen2.5:32b-instruct`). Empty/unset = no override."""
    override = os.environ.get("LUXE_MODEL_OVERRIDE", "").strip()
    return override or default_model


def make_backend(
    model: str,
    base_url: str = "http://127.0.0.1:11434",
    *,
    ignore_override: bool = False,
) -> Backend:
    # harness.backends.Backend posts to "/v1/chat/completions", so the
    # base_url must NOT include the /v1 suffix itself.
    #
    # `kind="mlx"` looks wrong when the default target is Ollama — it
    # isn't. `kind` is a label the harness uses for metrics/config
    # routing, not a transport selector. Ollama, MLX, llama-server, and
    # oMLX all expose OpenAI-compat endpoints, so the same Backend
    # client drives all four. `kind` stays "mlx" for continuity with the
    # harness's benchmark logging.
    #
    # OMLX_API_KEY is forwarded as a Bearer token whenever set. Ollama
    # and llama-server ignore unrecognized Authorization headers, so
    # passing it unconditionally is safe — and necessary when the
    # per-agent endpoint points at oMLX (which gates /v1/* on the key).
    #
    # `ignore_override=True` opts out of LUXE_BACKEND_OVERRIDE / _URL /
    # LUXE_MODEL_OVERRIDE. As of 2026-04-27 no caller passes True — all
    # agents (router/planner included) live on oMLX, so override and
    # canonical agree. Kept for future meta-orchestration callsites
    # that need to pin a model regardless of comparison-harness env vars.
    resolved_url = base_url if ignore_override else _resolve_override(base_url)
    resolved_model = model if ignore_override else _resolve_model_override(model)
    return Backend(
        kind="mlx",
        base_url=resolved_url,
        model_id=resolved_model,
        timeout_s=600.0,
        api_key=os.environ.get("OMLX_API_KEY", ""),
    )


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


_PARAMS_CACHE: dict[str, _CacheEntry] = {}
_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)[bB](?![a-zA-Z])")


def parameter_size(model: str, base_url: str) -> str:
    """Best-effort parameter count as a short string ("7B", "27B", "—").

    Tries Ollama's /api/show (reports e.g. "7.6B"), then llama-server's
    /props model_path, then a regex over the model name itself. Cached.
    """
    key = f"{base_url}::{model}"
    cached = _cache_get(_PARAMS_CACHE, key)
    if cached is not None:
        return cached

    # Ollama has the canonical field on `details.parameter_size`.
    try:
        r = httpx.post(f"{base_url}/api/show", json={"name": model}, timeout=5.0)
        r.raise_for_status()
        details = r.json().get("details") or {}
        ps = details.get("parameter_size")
        if isinstance(ps, str) and ps.strip():
            _cache_set(_PARAMS_CACHE, key, ps)
            return ps
    except httpx.HTTPError:
        pass

    # llama-server only exposes the file path — parse the filename for "-27b-".
    try:
        r = httpx.get(f"{base_url}/props", timeout=5.0)
        r.raise_for_status()
        path = (r.json() or {}).get("model_path") or ""
        m = _PARAM_RE.search(path)
        if m:
            result = f"{m.group(1)}B"
            _cache_set(_PARAMS_CACHE, key, result)
            return result
    except httpx.HTTPError:
        pass

    m = _PARAM_RE.search(model)
    result = f"{m.group(1)}B" if m else "—"
    _cache_set(_PARAMS_CACHE, key, result)
    return result


def max_context_length(model: str, base_url: str) -> int | None:
    """Model's native max context (distinct from the server's loaded ctx).
    Read from Ollama's /api/show model_info. llama-server doesn't expose
    this cleanly so we return None there."""
    try:
        r = httpx.post(f"{base_url}/api/show", json={"name": model}, timeout=5.0)
        r.raise_for_status()
        info = (r.json() or {}).get("model_info") or {}
        for k, v in info.items():
            if k.endswith(".context_length") and isinstance(v, int):
                return v
    except httpx.HTTPError:
        pass
    return None


def estimate_kv_ram_gb(model: str, ctx: int, base_url: str) -> float | None:
    """Rough fp16 KV-cache estimate in GiB, using architecture dims from
    Ollama's model_info. Returns None if the fields aren't available.

    Gemma 3 uses interleaved sliding-window + global attention, so the
    actual footprint is materially lower than this naive formula — the
    caller should display a disclaimer.
    """
    try:
        r = httpx.post(f"{base_url}/api/show", json={"name": model}, timeout=5.0)
        r.raise_for_status()
        info = (r.json() or {}).get("model_info") or {}
    except httpx.HTTPError:
        return None

    n_layers = n_kv_heads = head_dim = n_heads = embedding_length = None
    for k, v in info.items():
        if not isinstance(v, int):
            continue
        if k.endswith(".block_count"):
            n_layers = v
        elif k.endswith(".attention.head_count_kv"):
            n_kv_heads = v
        elif k.endswith(".attention.head_count"):
            n_heads = v
        elif k.endswith(".attention.key_length") or k.endswith(".attention.head_dim"):
            head_dim = v
        elif k.endswith(".embedding_length"):
            embedding_length = v

    # Not all families publish head_dim directly; derive it from
    # embedding_length / n_heads when missing (standard transformer).
    if head_dim is None and embedding_length and n_heads:
        head_dim = embedding_length // n_heads

    if not (n_layers and n_kv_heads and head_dim):
        return None
    bytes_per_token = 2 * n_layers * n_kv_heads * head_dim * 2  # K+V, fp16
    return (bytes_per_token * ctx) / (1024 ** 3)


def server_process_rss_gb(base_url: str) -> float | None:
    """Resident set size (GB) of the process listening on the endpoint's
    port. On macOS `psutil.net_connections()` needs root, so we iterate
    user-owned processes instead (Ollama/llama-server run as the user)."""
    try:
        import psutil
        from urllib.parse import urlparse
    except ImportError:
        return None
    port = urlparse(base_url).port
    if port is None:
        return None
    for proc in psutil.process_iter(["pid"]):
        try:
            for c in proc.net_connections(kind="inet"):
                if c.laddr and c.laddr.port == port and c.status == "LISTEN":
                    return proc.memory_info().rss / (1024 ** 3)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
    return None


def pull_stream(model: str, base_url: str) -> Iterator[dict]:
    """Stream decoded events from Ollama's /api/pull. Each event is one line
    of JSON with shape like {"status": "...", "digest": "...", "total": N,
    "completed": N} or {"error": "..."}. Raises httpx errors on transport
    failure; caller decides how to render."""
    with httpx.stream(
        "POST",
        f"{base_url}/api/pull",
        json={"name": model, "stream": True},
        timeout=None,
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def prewarm(model: str, base_url: str, *, timeout_s: float = 180.0) -> bool:
    """Best-effort model load via a 1-token completion. Works for both
    Ollama and llama-server since both accept /v1/chat/completions."""
    try:
        r = httpx.post(
            f"{base_url}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "temperature": 0,
                "stream": False,
            },
            timeout=timeout_s,
        )
        return r.status_code == 200
    except httpx.HTTPError:
        return False


_CTX_CACHE: dict[str, _CacheEntry] = {}
_DEFAULT_CTX = 8192


def clear_caches(model: str | None = None) -> None:
    """Invalidate cached ctx + param lookups. Call after `/pull` or when
    an agent's model tag changes mid-session so the banner and /context
    reflect the new weights. Passing `model` clears only entries for
    that tag; otherwise wipes everything."""
    if model is None:
        _CTX_CACHE.clear()
        _PARAMS_CACHE.clear()
        return
    for cache in (_CTX_CACHE, _PARAMS_CACHE):
        for key in list(cache.keys()):
            if key.endswith(f"::{model}"):
                del cache[key]


def context_length(model: str, base_url: str = "http://127.0.0.1:11434") -> int:
    """Declared context window for `model`. Tries Ollama's /api/show first,
    then llama-server's /props. Cached; falls back to 8192."""
    key = f"{base_url}::{model}"
    cached = _cache_get(_CTX_CACHE, key)
    if cached is not None:
        return cached

    # Try Ollama.
    try:
        r = httpx.post(f"{base_url}/api/show", json={"name": model}, timeout=5.0)
        r.raise_for_status()
        data = r.json()
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
        _cache_set(_CTX_CACHE, key, ctx)
        return ctx
    except httpx.HTTPError:
        pass

    # Try llama-server /props.
    try:
        r = httpx.get(f"{base_url}/props", timeout=5.0)
        r.raise_for_status()
        data = r.json()
        ctx = (data.get("default_generation_settings") or {}).get("n_ctx")
        if isinstance(ctx, int) and ctx > 0:
            _cache_set(_CTX_CACHE, key, ctx)
            return ctx
    except httpx.HTTPError:
        pass

    _cache_set(_CTX_CACHE, key, _DEFAULT_CTX)
    return _DEFAULT_CTX
