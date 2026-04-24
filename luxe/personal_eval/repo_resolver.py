"""Resolve a `--repo` value to a local path.

Accepts:
- Absolute or relative filesystem path → used as-is.
- GitHub shorthand `owner/repo` → cloned via `gh repo clone` into
  `personal_eval/cache/<owner>_<repo>/`.
- Full HTTPS or SSH URL → cloned via `gh repo clone` (handles auth) into
  the same cache directory.

Subsequent invocations reuse the cache and `git fetch --all --prune` to
pick up new merges. Full clones (not shallow) so arbitrary PR base_sha
values resolve.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent / "cache"

_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_URL_PREFIXES = ("http://", "https://", "git@", "ssh://", "git://")


def resolve_repo(spec: str) -> Path:
    """Return a local Path for `spec`, cloning if necessary."""
    if _looks_like_path(spec):
        p = Path(spec).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"repo path does not exist: {p}")
        return p

    owner_repo = _extract_owner_repo(spec)
    cache = CACHE_DIR / owner_repo.replace("/", "_")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if cache.exists():
        _git(["fetch", "--all", "--prune", "--tags"], cwd=cache)
    else:
        _clone(spec, cache)
    return cache


def _looks_like_path(spec: str) -> bool:
    if spec.startswith(_URL_PREFIXES):
        return False
    if spec.startswith(("/", "~", ".")):
        return True
    # Bare `owner/repo` form → clone. Anything with more slashes or
    # something filesystem-y → treat as path.
    return not _OWNER_REPO_RE.match(spec)


def _extract_owner_repo(spec: str) -> str:
    """Best-effort owner/repo extraction from URL or shorthand."""
    if spec.startswith(_URL_PREFIXES):
        # Strip protocol, trailing .git and any trailing slash.
        stripped = re.sub(r"^\w+://", "", spec)
        stripped = re.sub(r"^git@[^:]+:", "", stripped)  # git@github.com:owner/repo
        stripped = stripped.rstrip("/")
        if stripped.endswith(".git"):
            stripped = stripped[:-4]
        # The final two path segments are owner/repo.
        parts = [p for p in stripped.split("/") if p]
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1]}"
        raise ValueError(f"cannot parse owner/repo from url: {spec}")
    if _OWNER_REPO_RE.match(spec):
        return spec
    raise ValueError(f"unrecognized repo spec: {spec}")


def _clone(spec: str, dest: Path) -> None:
    # `gh repo clone` honors gh's auth for private repos. Full clone (no
    # --depth) so arbitrary PR base SHAs resolve.
    subprocess.run(  # noqa: S603
        ["gh", "repo", "clone", spec, str(dest)],
        check=True,
    )


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(  # noqa: S603
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
