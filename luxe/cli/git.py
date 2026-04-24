"""Tiny git helper for /review and /refactor.

Given a URL, find an existing clone in the current working directory whose
`origin` matches, or clone fresh into the cwd as a subdirectory. If a
matching clone exists, fast-forward pull. No destructive actions — we
bail rather than overwrite divergent work trees.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _run(cmd: list[str], cwd: Path | None = None, *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=check
    )


def _git_origin(repo: Path) -> str | None:
    try:
        r = _run(["git", "-C", str(repo), "config", "--get", "remote.origin.url"], check=False)
        return r.stdout.strip() or None
    except FileNotFoundError:
        return None


def normalize_url(url: str) -> str:
    """Canonicalize git URLs so `git@github.com:foo/bar.git` and
    `https://github.com/foo/bar` compare equal."""
    url = url.strip().lower().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    # SSH → HTTPS form for GitHub/GitLab/Bitbucket
    m = re.match(r"^git@([^:]+):(.+)$", url)
    if m:
        host, path = m.group(1), m.group(2)
        url = f"https://{host}/{path}"
    return url


def urls_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return normalize_url(a) == normalize_url(b)


def repo_name_from_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def find_local_clone(url: str, search_dir: Path) -> Path | None:
    """Scan `search_dir` one level deep for a git repo whose `origin`
    matches `url`. Returns the path if found, else None."""
    if not search_dir.exists():
        return None
    for entry in search_dir.iterdir():
        if not entry.is_dir():
            continue
        if not (entry / ".git").exists():
            continue
        if urls_match(_git_origin(entry), url):
            return entry
    return None


def fetch_and_update(repo: Path) -> tuple[bool, str]:
    """Fast-forward pull on an existing clone. Returns (ok, message).
    Bail on divergence rather than doing anything destructive."""
    try:
        r = _run(["git", "-C", str(repo), "pull", "--ff-only"], check=False)
    except FileNotFoundError:
        return False, "git not installed"
    if r.returncode == 0:
        return True, r.stdout.strip() or "already up to date"
    return False, (r.stderr.strip() or r.stdout.strip() or "pull failed")


def clone(url: str, target: Path) -> tuple[bool, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = _run(["git", "clone", url, str(target)], check=False)
    except FileNotFoundError:
        return False, "git not installed"
    if r.returncode == 0:
        return True, r.stdout.strip() or "clone ok"
    return False, (r.stderr.strip() or "clone failed")


def resolve_repo(url: str, search_dir: Path) -> tuple[Path | None, str]:
    """Main entry point: given a URL, return (local_path, status_message).

    1. Look for an existing matching clone in `search_dir`.
       → If found, `git pull --ff-only`.
    2. Otherwise, clone into `search_dir/<repo_name>`.
    """
    existing = find_local_clone(url, search_dir)
    if existing:
        ok, msg = fetch_and_update(existing)
        prefix = "updated" if ok else "found (pull failed)"
        return existing, f"{prefix}: {msg}"
    target = search_dir / repo_name_from_url(url)
    if target.exists():
        return None, f"target path {target} exists but origin does not match {url}"
    ok, msg = clone(url, target)
    if ok:
        return target, f"cloned fresh: {msg}"
    return None, f"clone failed: {msg}"
