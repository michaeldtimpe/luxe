"""Provider-key lookup for the web tools, cached per `/web` enable.

`luxe.secrets.resolve_api_key` walks env → ~/.luxe/secrets.env → Keychain,
and the Keychain step is a `security` subprocess. The chat layer asks which
web tools to offer on EVERY turn (search key, Tavily fallback, answers key),
so a host with no keys paid up to three subprocesses per turn to learn the
same "no" again. The answer is cached — misses included — until `/web` is
switched on again (`clear()`), which is also how a key added mid-session is
picked up. Key VALUES live only in this process's memory, never on disk.
"""

from __future__ import annotations

import threading

_CACHE: dict[str, str] = {}
_LOCK = threading.Lock()


def resolve(env_name: str) -> str:
    """The key for `env_name` ("" when none resolves), cached."""
    with _LOCK:
        if env_name in _CACHE:
            return _CACHE[env_name]
    from luxe.secrets import resolve_api_key
    try:
        value = resolve_api_key(env_name) or ""
    except Exception:
        value = ""
    with _LOCK:
        _CACHE[env_name] = value
    return value


def clear() -> None:
    """Forget every cached lookup (called when `/web` is switched on)."""
    with _LOCK:
        _CACHE.clear()
