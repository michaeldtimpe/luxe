"""Out-of-band repo metadata gathering for gitkit reports.

Everything here runs via `subprocess` in the runner (NOT through the agent's
tool surface), so the agent's read-only toolset stays byte-identical to the
benchmark path and `gh` never touches the shared bash allowlist. The gathered
text is injected into `run_single` as pure data under `<repo_health>` /
`<github_metadata>` tags (gitkit.sdd).

All git/gh calls degrade gracefully: a missing binary, a non-zero exit, a
timeout, an empty repo, or a non-GitHub remote collapses to a short note rather
than raising — the report is always produced from whatever is available.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from luxe.repo_index import build_repo_summary

_GIT_TIMEOUT = 30
_GH_TIMEOUT = 30

# Dependency manifests we surface so the model knows the dependency surface
# without walking the tree itself.
_MANIFESTS = (
    "requirements.txt", "pyproject.toml", "setup.py", "Pipfile",
    "package.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "Gemfile", "composer.json",
)


def _run_git(args: list[str], repo_path: str | Path,
             timeout: int = _GIT_TIMEOUT) -> tuple[bool, str]:
    """Run `git <args>` in `repo_path`. Returns (ok, output); ok is False on a
    missing binary, non-zero exit, or timeout (output then carries the reason)."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(repo_path),
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        return False, "git not found"
    except subprocess.TimeoutExpired:
        return False, f"git timed out after {timeout}s"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    return True, proc.stdout.strip()


def _run_gh(args: list[str], repo_path: str | Path,
            timeout: int = _GH_TIMEOUT) -> tuple[bool, str]:
    """Run `gh <args>` in `repo_path`. Returns (ok, output). Catches a missing
    `gh` binary, non-zero exit (incl. unauthenticated), and timeouts — any
    failure returns (False, reason) so callers can degrade to a placeholder."""
    try:
        proc = subprocess.run(
            ["gh", *args], cwd=str(repo_path),
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        return False, "gh CLI not installed"
    except subprocess.TimeoutExpired:
        return False, f"gh timed out after {timeout}s"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    return True, proc.stdout.strip()


def current_head(repo_path: str | Path) -> str:
    """Return the current HEAD sha (short), or "" if unavailable (empty repo)."""
    ok, out = _run_git(["rev-parse", "--short", "HEAD"], repo_path)
    return out if ok else ""


def _size_lines(repo_path: str | Path) -> list[str]:
    """Repo size signals (files / LOC / language mix) via build_repo_summary."""
    try:
        s = build_repo_summary(repo_path)
    except Exception:  # noqa: BLE001 — size is best-effort, never fatal
        return []
    if not s.file_count:
        return ["Files: 0 (no recognized source files)"]
    lines = [f"Files: {s.file_count}", f"Tracked LOC: ~{s.total_loc:,}"]
    top = sorted(s.languages.items(), key=lambda kv: -kv[1])[:5]
    if top:
        lines.append("Top languages (by file count):")
        for lang, count in top:
            pct = round(100 * count / s.file_count)
            loc = s.languages_loc.get(lang, 0)
            lines.append(f"  - {lang}: {pct}% ({count} files, {loc:,} LOC)")
    return lines


def _manifest_lines(repo_path: str | Path) -> list[str]:
    root = Path(repo_path)
    found = [m for m in _MANIFESTS if (root / m).is_file()]
    return [f"Dependency manifests: {', '.join(found)}"] if found else []


def gather_repo_health(repo_path: str | Path) -> str:
    """Build the `<repo_health>` data block from local git history + repo size.

    Args:
        repo_path: path to the (already-resolved, local) git repo.

    Returns:
        A `<repo_health>...</repo_health>` markdown block (always non-empty). A
        repo with no commits yields a one-line "blank repository" note plus any
        size/manifest signals. Side effects: read-only git/filesystem reads.
    """
    lines: list[str] = []
    lines.extend(_size_lines(repo_path))

    ok_count, count = _run_git(["rev-list", "--count", "HEAD"], repo_path)
    if not ok_count:
        # Empty repo (no commits yet) or not a git repo — still emit size info.
        lines.append("History: blank repository (no commits yet) "
                     "or not a git working tree.")
        lines.extend(_manifest_lines(repo_path))
        body = "\n".join(lines) if lines else "No repository signals available."
        return f"<repo_health>\n{body}\n</repo_health>"

    lines.append(f"Total commits: {count}")
    ok_merges, merges = _run_git(["rev-list", "--count", "--merges", "HEAD"], repo_path)
    if ok_merges:
        lines.append(f"Merge commits (local merged-PR proxy): {merges}")
    for days in (30, 90):
        ok_w, win = _run_git(
            ["rev-list", "--count", f"--since={days}.days", "HEAD"], repo_path)
        if ok_w:
            lines.append(f"Commits in last {days} days: {win}")

    ok_dates, dates = _run_git(["log", "--format=%ai"], repo_path)
    if ok_dates and dates:
        rows = dates.splitlines()
        lines.append(f"Latest commit: {rows[0]}")
        lines.append(f"First commit: {rows[-1]}")

    ok_auth, auth = _run_git(["log", "--format=%an"], repo_path)
    if ok_auth and auth:
        lines.append(f"Distinct authors: {len(set(auth.splitlines()))}")

    ok_subj, subj = _run_git(["log", "-15", "--format=%s"], repo_path)
    if ok_subj and subj:
        lines.append("Recent commit subjects:")
        lines.extend(f"  - {s}" for s in subj.splitlines())

    lines.extend(_manifest_lines(repo_path))
    body = "\n".join(lines)
    return f"<repo_health>\n{body}\n</repo_health>"


def _is_github_remote(repo_path: str | Path) -> bool:
    ok, url = _run_git(["remote", "get-url", "origin"], repo_path)
    return ok and "github.com" in url


def gather_github_metadata(repo_path: str | Path) -> str:
    """Build the `<github_metadata>` block via the `gh` CLI, capped + graceful.

    Gathers `gh repo view` first; only if that succeeds does it pull capped
    summary stats (merged/open PRs, issues, releases) — the model needs counts
    and recency, not raw dumps. Any failure (gh missing, unauthenticated,
    non-GitHub remote, timeout) returns a one-line placeholder.

    Args:
        repo_path: path to the (already-resolved, local) git repo.

    Returns:
        A `<github_metadata>...</github_metadata>` markdown block (always
        non-empty). Side effects: read-only `gh` API calls when available.
    """
    def _wrap(body: str) -> str:
        return f"<github_metadata>\n{body}\n</github_metadata>"

    if not _is_github_remote(repo_path):
        return _wrap("GitHub metadata unavailable (no GitHub `origin` remote).")

    ok, view = _run_gh(
        ["repo", "view", "--json",
         "nameWithOwner,description,stargazerCount,forkCount,licenseInfo,"
         "isArchived,pushedAt,primaryLanguage,openGraphImageUrl"],
        repo_path)
    if not ok:
        return _wrap(f"GitHub metadata unavailable ({view}). "
                     "Report uses local git only.")

    lines = ["Repository (via gh):", view]

    # Capped summary stats — counts/recency, not full lists.
    ok_m, merged = _run_gh(
        ["pr", "list", "--state", "merged", "--limit", "20",
         "--json", "number,title,mergedAt"], repo_path)
    if ok_m:
        lines.append(f"Recent merged PRs (capped 20): {merged}")
    ok_o, open_pr = _run_gh(
        ["pr", "list", "--state", "open", "--limit", "20", "--json", "number,title"],
        repo_path)
    if ok_o:
        lines.append(f"Open PRs (capped 20): {open_pr}")
    ok_i, issues = _run_gh(
        ["issue", "list", "--state", "open", "--limit", "20", "--json", "number,title"],
        repo_path)
    if ok_i:
        lines.append(f"Open issues (capped 20): {issues}")
    ok_r, rels = _run_gh(["release", "list", "--limit", "5"], repo_path)
    if ok_r and rels:
        lines.append(f"Recent releases (capped 5):\n{rels}")

    return _wrap("\n".join(lines))


def gather_context(repo_path: str | Path) -> str:
    """Concatenate the repo-health + GitHub-metadata data blocks for injection
    into `run_single(extra_context=...)`. Pure data, no instructions."""
    return gather_repo_health(repo_path) + "\n\n" + gather_github_metadata(repo_path)
