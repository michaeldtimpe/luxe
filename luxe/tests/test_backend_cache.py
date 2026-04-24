"""Tests for the TTL cache helpers in luxe.backend.

Covers the `_CacheEntry` wrapper, `_cache_get` / `_cache_set`, TTL
expiry behaviour, and the invariant that `clear_caches()` still
wipes everything even after the wrapper change.
"""

from __future__ import annotations

import cli.backend as backend
from cli.backend import (
    _CacheEntry,
    _cache_get,
    _cache_set,
    _CTX_CACHE,
    _PARAMS_CACHE,
    clear_caches,
)


def _reset() -> None:
    _CTX_CACHE.clear()
    _PARAMS_CACHE.clear()


def test_cache_set_then_get_returns_value():
    _reset()
    _cache_set(_PARAMS_CACHE, "k", "7B")
    assert _cache_get(_PARAMS_CACHE, "k") == "7B"


def test_cache_get_missing_returns_none():
    _reset()
    assert _cache_get(_PARAMS_CACHE, "nope") is None


def test_cache_expired_returns_none_and_evicts(monkeypatch):
    _reset()
    # Freeze time to t=100, write entry (expires at 100 + TTL), jump past.
    times = [100.0]
    monkeypatch.setattr(backend.time, "monotonic", lambda: times[0])
    _cache_set(_PARAMS_CACHE, "k", "7B")
    # Sanity — still valid within TTL.
    assert _cache_get(_PARAMS_CACHE, "k") == "7B"
    times[0] = 100.0 + backend._CACHE_TTL_S + 1.0
    assert _cache_get(_PARAMS_CACHE, "k") is None
    # Expired entry was evicted, not just shadowed.
    assert "k" not in _PARAMS_CACHE


def test_cache_entry_fields():
    _reset()
    _cache_set(_CTX_CACHE, "k", 32768)
    entry = _CTX_CACHE["k"]
    assert isinstance(entry, _CacheEntry)
    assert entry.value == 32768
    assert entry.expires_at > 0


def test_clear_caches_wipes_everything():
    _reset()
    _cache_set(_PARAMS_CACHE, "http://x::m", "7B")
    _cache_set(_CTX_CACHE, "http://x::m", 8192)
    clear_caches()
    assert _PARAMS_CACHE == {}
    assert _CTX_CACHE == {}


def test_clear_caches_by_model_is_selective():
    _reset()
    _cache_set(_PARAMS_CACHE, "http://x::alpha", "7B")
    _cache_set(_PARAMS_CACHE, "http://x::beta", "13B")
    clear_caches(model="alpha")
    assert "http://x::alpha" not in _PARAMS_CACHE
    assert _PARAMS_CACHE.get("http://x::beta") is not None
    assert _cache_get(_PARAMS_CACHE, "http://x::beta") == "13B"
