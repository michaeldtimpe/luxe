"""Fault-tolerant repo-tree walking.

`Path.rglob()` is NOT safe to run over an arbitrary user-chosen root: CPython's
pathlib swallows `PermissionError` from `os.scandir()` and nothing else, so any
other `OSError` propagates out of the generator and kills the caller.

That is not hypothetical. Network-backed directories (Synology Drive / iCloud /
OneDrive placeholder trees, stale SMB or NFS mounts) raise
`OSError(ETIMEDOUT, "Operation timed out")` from `scandir` when the server is
unreachable. Running `luxe chat` from `$HOME` — the documented `luxe-chat`
workflow — puts `~/Library/CloudStorage/...` inside the "repo", so the first
turn's `find_all_sdd()` walk died ~3s in and took the whole Textual app with it
(2026-07-29; see `lessons.md`).

`os.walk()` is tolerant by construction: `scandir` failures are routed to the
`onerror` callback and the walk continues with the next directory. This module
wraps it with the pruning every caller wants (VCS/vendor/build dirs) plus DEBUG
logging of what was skipped, so an unreadable subtree degrades to "those files
weren't found" instead of a crash.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Iterator

logger = logging.getLogger(__name__)

# Directories never worth descending into for contract/citation lookups. Kept in
# sync (by intent, not by import) with repo_index._DEFAULT_EXCLUDES and
# citations._BARE_FILENAME_EXCLUDE_DIRS.
DEFAULT_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", "target", ".next", ".nuxt", ".tox",
    ".pytest_cache", ".ruff_cache", ".mypy_cache",
})


def _log_walk_error(exc: OSError) -> None:
    # Unreadable / unreachable directory: skip it, keep walking. DEBUG (not
    # WARNING) because a home-directory walk legitimately hits dozens of these.
    logger.debug("fswalk: skipping %s (%s)", getattr(exc, "filename", "?"), exc)


def iter_files(
    root: str | Path,
    *,
    skip_dirs: frozenset[str] | set[str] = DEFAULT_SKIP_DIRS,
    name_filter: Callable[[str], bool] | None = None,
) -> Iterator[Path]:
    """Yield files under `root`, skipping unreadable directories.

    `skip_dirs` prunes directories by name at every level. `name_filter`, when
    given, is applied to the BASENAME so the common case (match by suffix or by
    exact filename) never builds a `Path` for files it will discard.

    Order is `os.walk`'s (arbitrary); callers that need determinism sort the
    result — as `find_all_sdd` does.
    """
    top = Path(root)
    for cur, dirs, files in os.walk(top, onerror=_log_walk_error):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            if name_filter is not None and not name_filter(name):
                continue
            yield Path(cur) / name
