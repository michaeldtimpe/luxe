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
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

logger = logging.getLogger(__name__)

# Directories never worth descending into for contract/citation lookups. Kept in
# sync (by intent, not by import) with repo_index._DEFAULT_EXCLUDES and
# citations._BARE_FILENAME_EXCLUDE_DIRS.
DEFAULT_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", "target", ".next", ".nuxt", ".tox",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "site-packages",
})

# Pruned ONLY at the top level of the scan root: these are macOS home-directory
# fixtures, not project directories. `~/Library` alone holds ~488k files here —
# half the cost of indexing a home directory — and none of it is source the user
# means to search. Never pruned deeper, so a repo with its own `Library/` is
# indexed normally.
HOME_NOISE_DIRS = frozenset({
    "Library", "Applications", "Movies", "Music", "Pictures", "Public",
    "Trash", ".Trash",
})


def _log_walk_error(exc: OSError) -> None:
    # Unreadable / unreachable directory: skip it, keep walking. DEBUG (not
    # WARNING) because a home-directory walk legitimately hits dozens of these.
    logger.debug("fswalk: skipping %s (%s)", getattr(exc, "filename", "?"), exc)


def _git_tracked_files(root: Path) -> list[Path] | None:
    """Every file git knows about under `root` (tracked + untracked-but-not-ignored),
    or None when `root` isn't a git work tree.

    This is the fast path AND the correct one: `.gitignore` already encodes
    "don't look here" for virtualenvs, build output, vendored trees, and data
    dumps — knowledge a name-based prune list can only approximate. It costs
    ~10ms where walking the same repo costs seconds.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    names = proc.stdout.decode("utf-8", errors="replace").split("\0")
    return [root / n for n in names if n]


@dataclass
class SourceScan:
    """The file list an index should be built from, plus what it cost/skipped."""

    root: Path
    paths: list[Path] = field(default_factory=list)
    seconds: float = 0.0
    used_git: bool = False
    truncated: str = ""        # non-empty: which cap stopped the scan
    oversized: int = 0         # files skipped for exceeding max_file_bytes

    @property
    def count(self) -> int:
        return len(self.paths)


def scan_source_files(
    root: str | Path,
    *,
    extensions: frozenset[str] | set[str],
    max_files: int = 0,
    max_file_bytes: int = 256 * 1024,
    max_total_bytes: int = 0,
    skip_dirs: frozenset[str] | set[str] = DEFAULT_SKIP_DIRS,
    top_level_skip: frozenset[str] | set[str] = HOME_NOISE_DIRS,
    use_git: bool = True,
    on_progress: Callable[[int], None] | None = None,
) -> SourceScan:
    """Enumerate indexable source files under `root`, ONCE, with hard bounds.

    Shared by the BM25 and symbol indexes so a chat startup walks the tree once
    instead of twice. Bounded because the root is user-chosen: `luxe chat` from
    `$HOME` sees ~1M files / 56k candidates, which is minutes of tokenizing and
    parsing. `max_files` / `max_total_bytes` stop the scan and record WHY in
    `truncated` — callers must surface that (no silent caps).

    `top_level_skip` names are pruned only at depth 1, so pointing luxe at a
    home directory skips `Library` and friends without ever pruning a `Library/`
    that belongs to a real repo.
    """
    root_path = Path(root).resolve()
    t0 = time.monotonic()
    scan = SourceScan(root=root_path)
    total_bytes = 0

    def _accept(p: Path) -> bool:
        """Size-filter one candidate and fold it into the scan. False = stop."""
        nonlocal total_bytes
        try:
            size = p.stat().st_size
        except OSError:
            return True
        if size > max_file_bytes:
            scan.oversized += 1
            return True
        if max_total_bytes and total_bytes + size > max_total_bytes:
            scan.truncated = f"{max_total_bytes // (1024 * 1024)} MB byte budget"
            return False
        total_bytes += size
        scan.paths.append(p)
        if on_progress and len(scan.paths) % 500 == 0:
            on_progress(len(scan.paths))
        if max_files and len(scan.paths) >= max_files:
            scan.truncated = f"{max_files}-file cap"
            return False
        return True

    listed = _git_tracked_files(root_path) if use_git else None
    if listed is not None:
        scan.used_git = True
        for p in listed:
            if p.suffix.lower() in extensions and not _accept(p):
                break
    else:
        # BREADTH-first, not os.walk's depth-first: when a cap truncates the
        # scan, shallow files are the ones worth having. Depth-first would spend
        # the whole budget inside whichever deep subtree happened to sort first
        # (from `~`, that was a random corner of Downloads).
        queue: deque[Path] = deque([root_path])
        while queue:
            cur = queue.popleft()
            try:
                with os.scandir(cur) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError as e:
                _log_walk_error(e)
                continue
            subdirs: list[Path] = []
            stop = False
            for entry in entries:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                # `follow_symlinks=False`: a symlinked directory is not
                # descended into, which is also the cheapest loop guard.
                if is_dir:
                    name = entry.name
                    if (name in skip_dirs
                            or (cur == root_path and name in top_level_skip)
                            or (name.startswith(".") and name != ".github")):
                        continue
                    subdirs.append(Path(entry.path))
                elif Path(entry.name).suffix.lower() in extensions:
                    if not _accept(Path(entry.path)):
                        stop = True
                        break
            if stop:
                break
            queue.extend(subdirs)

    scan.seconds = time.monotonic() - t0
    if on_progress:
        on_progress(len(scan.paths))
    return scan


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
