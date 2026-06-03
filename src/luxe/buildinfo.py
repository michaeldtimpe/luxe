"""Build/version info for the chat startup banner (C3).

Shows which exact build produced a run (invaluable when debugging autonomous
goal behavior) and a concise, offline-safe "behind origin" hint. All git calls
target the luxe SOURCE repo (not the user's working repo) and degrade silently.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from luxe import __version__


def _repo_root() -> Path:
    # src/luxe/buildinfo.py → parents[2] == repo root
    return Path(__file__).resolve().parents[2]


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(_repo_root()), *args],
            capture_output=True, text=True, timeout=2,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def version_string() -> str:
    """`<short-sha>[+dirty]`, falling back to the static __version__."""
    sha = _git("rev-parse", "--short", "HEAD")
    if not sha:
        return __version__
    dirty = _git("status", "--porcelain")
    return f"{sha}+dirty" if dirty else sha


def behind_origin(ref: str = "origin/main") -> int:
    """Commits local HEAD is behind `ref`, using already-fetched refs (NO
    network). Returns 0 when up-to-date, offline, or not a git/remote checkout."""
    n = _git("rev-list", "--count", f"HEAD..{ref}")
    try:
        return int(n) if n else 0
    except ValueError:
        return 0


def _count(rev_range: str) -> int:
    n = _git("rev-list", "--count", rev_range)
    try:
        return int(n) if n else 0
    except ValueError:
        return 0


def build_status_hint() -> str | None:
    """A single most-actionable line about the luxe SOURCE checkout, or None when
    clean & current. Local refs only — no network (honors the no-`origin/<branch>`
    -fetch rule). Priority: behind > unpushed > dirty.

    The no-upstream case is guarded: `@{upstream}` errors when the branch has no
    tracking ref, and `_git` degrades that to None → count 0 → skipped.
    """
    behind = behind_origin()
    if behind:
        return f"{behind} behind origin/main — git pull"
    if _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"):
        ahead = _count("@{upstream}..HEAD")
        if ahead:
            return f"{ahead} ahead — git push"
    if _git("status", "--porcelain"):
        return "uncommitted changes"
    return None
