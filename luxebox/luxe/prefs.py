"""User-level preferences stored under ~/.luxe/ — bookmarks, memory, aliases.

Distinct from the project config at configs/agents.yaml (which ships with
the repo) and from session JSONL logs (per-conversation history).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

PREFS_DIR = Path("~/.luxe").expanduser()
BOOKMARKS_FILE = PREFS_DIR / "bookmarks.json"
MEMORY_FILE = PREFS_DIR / "memory.md"
ALIASES_FILE = PREFS_DIR / "aliases.yaml"
MEMORY_MAX_CHARS = 2000


def _ensure_dir() -> None:
    PREFS_DIR.mkdir(parents=True, exist_ok=True)


# ── Bookmarks ───────────────────────────────────────────────────────────

def load_bookmarks() -> dict[str, str]:
    if not BOOKMARKS_FILE.exists():
        return {}
    try:
        data = json.loads(BOOKMARKS_FILE.read_text())
        return {str(k): str(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError):
        return {}


def save_bookmark(name: str, session_id: str) -> None:
    _ensure_dir()
    data = load_bookmarks()
    data[name] = session_id
    BOOKMARKS_FILE.write_text(json.dumps(data, indent=2, sort_keys=True))


def remove_bookmark(name: str) -> bool:
    data = load_bookmarks()
    if name not in data:
        return False
    del data[name]
    BOOKMARKS_FILE.write_text(json.dumps(data, indent=2, sort_keys=True))
    return True


def resolve_session_key(key: str, session_root: Path) -> Path | None:
    """Resolve `key` to a session JSONL path.

    Order: bookmark name → exact session id → unique prefix match.
    Returns None if ambiguous or unknown.
    """
    bookmarks = load_bookmarks()
    if key in bookmarks:
        key = bookmarks[key]
    exact = session_root / f"{key}.jsonl"
    if exact.exists():
        return exact
    candidates = sorted(session_root.glob(f"{key}*.jsonl"))
    if len(candidates) == 1:
        return candidates[0]
    return None


# ── Memory ──────────────────────────────────────────────────────────────

def load_memory() -> str:
    if not MEMORY_FILE.exists():
        return ""
    text = MEMORY_FILE.read_text()
    if len(text) > MEMORY_MAX_CHARS:
        text = text[:MEMORY_MAX_CHARS] + "\n... [memory truncated]"
    return text


def write_memory(text: str) -> None:
    _ensure_dir()
    MEMORY_FILE.write_text(text)


def clear_memory() -> None:
    if MEMORY_FILE.exists():
        MEMORY_FILE.unlink()


# ── Aliases ─────────────────────────────────────────────────────────────

def load_aliases() -> dict[str, str]:
    if not ALIASES_FILE.exists():
        return {}
    try:
        data = yaml.safe_load(ALIASES_FILE.read_text()) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_alias(name: str, expansion: str) -> None:
    _ensure_dir()
    data = load_aliases()
    data[name] = expansion
    ALIASES_FILE.write_text(yaml.safe_dump(data, sort_keys=True))


def remove_alias(name: str) -> bool:
    data = load_aliases()
    if name not in data:
        return False
    del data[name]
    ALIASES_FILE.write_text(yaml.safe_dump(data, sort_keys=True) if data else "")
    return True
