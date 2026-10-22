"""`/diff` for `luxe chat`: what this session changed in the repo.

Lives below both `inspection` (which re-exports it for `/diff`) and `doctor`
(whose `working tree` line counts the same files), so neither has to import
the other's facade to reach it. Read-only.
"""

from __future__ import annotations

from dataclasses import dataclass

from luxe import gitcmd

@dataclass
class FileDiff:
    path: str
    added: int = 0
    removed: int = 0
    untracked: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.untracked)


# `luxe.gitcmd.run_ok` IS this function's old body, verbatim — it moved there
# when the eight git wrappers were consolidated (2026-08-04). Kept as a local
# name so the six call sites below read the same.
_git = gitcmd.run_ok


def session_diff(repo_path: str, paths: list[str] | None = None
                 ) -> tuple[list[FileDiff], str]:
    """Per-file +/- counts for `paths` (or the whole working tree when None).

    Compares against HEAD so staged AND unstaged edits both show; untracked
    files are reported as new rather than silently omitted (a session that
    scaffolded a file has changed the repo even though git has no baseline for
    it). Returns (diffs, error) — `error` non-empty means git couldn't answer.
    """
    if not repo_path:
        return [], "no repo for this session"
    args = ["diff", "--numstat", "HEAD"]
    if paths:
        args += ["--", *paths]
    ok, out = _git(repo_path, *args)
    if not ok:
        # No commits yet → no HEAD to diff against; fall back to the index.
        ok, out = _git(repo_path, "diff", "--numstat",
                       *(["--", *paths] if paths else []))
        if not ok:
            return [], out or "git diff failed"

    diffs: dict[str, FileDiff] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        diffs[path] = FileDiff(
            path=path,
            added=0 if added == "-" else int(added or 0),
            removed=0 if removed == "-" else int(removed or 0),
        )

    ok, status = _git(repo_path, "status", "--porcelain", "--untracked-files=normal")
    if ok:
        wanted = set(paths or [])
        for line in status.splitlines():
            if not line.startswith("?? "):
                continue
            path = line[3:].strip()
            if wanted and path not in wanted:
                continue
            diffs.setdefault(path, FileDiff(path=path, untracked=True))
    return [diffs[k] for k in sorted(diffs)], ""


def full_diff(repo_path: str, paths: list[str] | None = None) -> str:
    """The patch itself, for `/diff --full`."""
    args = ["diff", "HEAD"]
    if paths:
        args += ["--", *paths]
    ok, out = _git(repo_path, *args, timeout=20.0)
    if not ok:
        ok, out = _git(repo_path, "diff", *(["--", *paths] if paths else []),
                       timeout=20.0)
    return out if ok else ""
