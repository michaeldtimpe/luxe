"""Per-(repo, mode) cache for the planner's subtask decomposition.

Why: the router runs at temperature=0.1, which is non-zero by design (the
greedy 0.0 mode degenerates to the single-subtask fallback for ambiguous
goals). That non-determinism makes cross-backend `/review` comparisons
"same goal, possibly slightly different decomposition" rather than
"identical plan, two backends". Caching the parsed subtask list per
(repo_path, mode) lets reruns reuse the same decomposition.

Stored as one JSON file per cache key under ~/.luxe/plan_cache/. Schema:
    {"repo": "<repo_path>", "mode": "<review|refactor>",
     "stored_at": <unix_ts>, "entries": [{"title": ..., "agent": ...}, ...]}

TTL defaults to 24h. Pass use_cache=False to bypass for a fresh
decomposition.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

CACHE_DIR = Path.home() / ".luxe" / "plan_cache"
DEFAULT_TTL_S = 24 * 60 * 60

# Bump when build_review_goal text changes meaningfully so existing
# cached plans auto-invalidate. Without this, a goal-text edit (e.g.
# tightening sub 02 to skip absent docs files) would have no effect on
# repos already planned within the 24h TTL — the cache would keep
# handing back the old decomposition.
_GOAL_VERSION = "v2-2026-04-27-tighten-orient"


def _path_for(repo: str, mode: str) -> Path:
    h = hashlib.sha1(f"{repo}|{mode}|{_GOAL_VERSION}".encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{h}.json"


def lookup(repo: str, mode: str, ttl_s: float = DEFAULT_TTL_S) -> list[dict] | None:
    """Return cached entries if present and within TTL; else None."""
    path = _path_for(repo, mode)
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    stored_at = data.get("stored_at")
    if not isinstance(stored_at, (int, float)):
        return None
    if time.time() - stored_at > ttl_s:
        return None
    entries = data.get("entries")
    return entries if isinstance(entries, list) else None


def store(repo: str, mode: str, entries: list[dict]) -> None:
    """Persist entries for (repo, mode). Best-effort — failures swallowed
    so caching is never load-bearing for the production path."""
    if not entries:
        return
    path = _path_for(repo, mode)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "repo": repo,
            "mode": mode,
            "stored_at": time.time(),
            "entries": entries,
        }, indent=2))
    except OSError:
        pass


def clear(repo: str | None = None, mode: str | None = None) -> int:
    """Drop entries. With no args, clears the whole cache directory. With
    just `repo`, clears every mode for that repo. Returns count removed."""
    if not CACHE_DIR.exists():
        return 0
    if repo and mode:
        path = _path_for(repo, mode)
        if path.exists():
            path.unlink()
            return 1
        return 0
    targets: list[Path] = []
    for f in CACHE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            targets.append(f)
            continue
        if repo and data.get("repo") != repo:
            continue
        if mode and data.get("mode") != mode:
            continue
        targets.append(f)
    for t in targets:
        try:
            t.unlink()
        except OSError:
            pass
    return len(targets)
