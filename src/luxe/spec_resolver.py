"""Spec resolver — assemble `.sdd` chains for a target file (Lever 2).

Given a repo root and a target path, walks from the target's containing
directory up to the repo root, collecting `<dir>/<dir>.sdd` contracts at
each step. Returns an ancestor-first chain — the closest ancestor `.sdd`
to the repo root comes first, the closest to the target comes last —
so leaf-level rules can override or extend root-level invariants.

The convention: each directory `<dir>/` may contain `<dir>/<dir>.sdd`
where the file's basename matches the directory's name. Directories
without a matching `.sdd` are silently skipped.

Globs in `Owns:` / `Forbids:` / `Depends on:` sections are interpreted
relative to the **repo root**, not relative to the `.sdd`'s containing
directory. This keeps glob semantics consistent across all `.sdd` files
in the chain — a `Forbids: tests/**` in `src/luxe/luxe.sdd` means the
same thing as the same glob written in `src/luxe/agents/agents.sdd`.

Glob syntax (gitignore-style, hand-rolled because pathlib's `match` does
not support `**` until Python 3.13):
    *       — any chars except `/`
    **      — any chars including `/` (i.e. crosses directories)
    ?       — single char except `/`
    [...]   — character class
Other regex metacharacters are escaped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from luxe.sdd import SddFile, parse_sdd_file


@dataclass(frozen=True)
class ResolvedChain:
    """Ancestor-first chain of `.sdd` contracts for a single target.

    `target_rel` is the target's path **relative to the repo root**, used
    for glob matching. `files` lists the chain ancestor-first; the empty
    list means no `.sdd` exists between the target and the repo root.
    """

    repo_root: Path
    target_rel: str
    files: list[SddFile] = field(default_factory=list)

    def is_forbidden(self, rel_path: str) -> tuple[bool, SddFile | None, str | None]:
        """Check `rel_path` (repo-root-relative) against every Forbids glob.

        Returns `(True, sdd_file, glob)` for the first hit found in the
        chain (root → leaf order), or `(False, None, None)` if the path
        is not forbidden. Path normalization: leading `./` and `/` are
        stripped; backslashes are converted to forward slashes.
        """
        normalized = _normalize_rel(rel_path)
        for sf in self.files:
            for glob in sf.forbids:
                if _glob_matches(glob, normalized):
                    return True, sf, glob
        return False, None, None

    def is_owned(self, rel_path: str) -> tuple[bool, SddFile | None, str | None]:
        """Check `rel_path` against every Owns glob.

        Returns `(True, sdd_file, glob)` for the first hit, or
        `(False, None, None)` if no ancestor claims ownership. An empty
        chain (no `.sdd` files) returns `(False, None, None)` — callers
        decide whether that's a violation (Lever 2 says "warning only;
        Phase 3 promotes to strict").
        """
        normalized = _normalize_rel(rel_path)
        for sf in self.files:
            for glob in sf.owns:
                if _glob_matches(glob, normalized):
                    return True, sf, glob
        return False, None, None

    def all_forbids(self) -> list[tuple[SddFile, str]]:
        """Flat list of (source_file, glob) pairs across the chain.

        Useful for prompt construction — the worker prompt lists every
        Forbids rule with its source so the model can reason about why
        a path is off-limits.
        """
        out: list[tuple[SddFile, str]] = []
        for sf in self.files:
            for glob in sf.forbids:
                out.append((sf, glob))
        return out


def resolve_chain(repo_root: Path, target: Path) -> ResolvedChain:
    """Walk from `target`'s containing dir up to `repo_root`, collecting `.sdd`.

    `target` may be a file or a directory; if it's a file, the walk
    starts at its parent. Both arguments are resolved to absolute paths.
    Raises `ValueError` if `target` is not under `repo_root`.

    Each `.sdd` parsed propagates `SddParseError` to the caller — the
    resolver does not silently skip malformed contracts. Authoring a
    broken `.sdd` is a hard error that should be visible at the first
    iteration that touches the file.
    """
    root = repo_root.resolve()
    tgt = target.resolve()

    try:
        rel = tgt.relative_to(root)
    except ValueError as e:
        raise ValueError(
            f"target {tgt} is not inside repo_root {root}"
        ) from e

    files: list[SddFile] = []
    for directory in _walk_up(tgt, root):
        candidate = directory / f"{directory.name}.sdd"
        if candidate.is_file():
            files.append(parse_sdd_file(candidate))

    files.reverse()  # ancestor-first
    return ResolvedChain(
        repo_root=root,
        target_rel=str(rel).replace("\\", "/"),
        files=files,
    )


# --- internals ----------------------------------------------------------


def _walk_up(target: Path, root: Path) -> Iterator[Path]:
    """Yield directories from target's parent up to (and including) root.

    If `target` is a directory, the walk starts at `target` itself; if
    it's a file (or doesn't exist), the walk starts at its parent. The
    walk stops the first time it would go above `root`.
    """
    current = target if target.is_dir() else target.parent
    while True:
        yield current
        if current == root:
            return
        parent = current.parent
        if parent == current:  # filesystem root reached without hitting repo_root
            return
        current = parent


def _normalize_rel(rel: str) -> str:
    """Normalize a relative path for glob matching.

    Strips leading `./` and `/`, converts backslashes to forward slashes.
    Trailing slashes are preserved — `tests/` and `tests` may match
    different globs.
    """
    s = rel.replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    while s.startswith("/"):
        s = s[1:]
    return s


def _glob_matches(glob: str, path: str) -> bool:
    """Match `path` against a gitignore-style `glob`.

    See module docstring for syntax. Cached pattern compilation makes
    repeat checks across a long chain cheap.
    """
    return _compile_glob(glob).match(path) is not None


def _compile_glob(glob: str) -> re.Pattern[str]:
    """Translate a gitignore-style glob to a compiled regex.

    `**` consumes path separators; `*`/`?` do not. Bracket expressions
    are passed through unchanged (Python's regex character classes are
    a strict superset of glob's).
    """
    cached = _GLOB_CACHE.get(glob)
    if cached is not None:
        return cached

    out: list[str] = []
    i = 0
    n = len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                # `**` — match any chars including separators
                out.append(".*")
                i += 2
                # Consume an immediately-following `/` so `foo/**/bar`
                # matches `foo/bar` (no intermediate dir) as well as
                # `foo/x/y/bar`.
                if i < n and glob[i] == "/":
                    i += 1
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            # Bracket expression — find the matching `]` and pass through.
            # Negation `[!...]` is rewritten to regex `[^...]`.
            j = glob.find("]", i + 1)
            if j == -1:
                # Malformed bracket — treat as literal
                out.append(re.escape(c))
                i += 1
                continue
            body = glob[i + 1 : j]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append(f"[{body}]")
            i = j + 1
        elif c in r".+()^$|\\":
            out.append("\\" + c)
            i += 1
        else:
            out.append(c)
            i += 1

    pattern = re.compile("^" + "".join(out) + "$")
    _GLOB_CACHE[glob] = pattern
    return pattern


_GLOB_CACHE: dict[str, re.Pattern[str]] = {}
