"""Fallback ingester: synthesize PRRecord-shaped tasks from git history.

For solo / light-review repos where `gh pr list` returns nothing useful.
Each non-merge commit on the default branch becomes one task:

- base_sha = commit^  (the state we check out for the write replay)
- head_sha = commit   (the ground-truth state)
- title    = commit subject
- body     = commit body (if any; else the subject)
- added/deleted = from `git show --stat --numstat`
- review_comments = []  (no human review data; B1 scoring becomes unscored)

Output JSON lives at the same `personal_eval/corpus/<repo>.json` path so
the rest of Phase B reads it transparently.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from personal_eval.gh_ingest import CORPUS_DIR, PRRecord


def ingest_from_commits(
    repo_path: Path,
    language: str,
    *,
    max_commits: int = 50,
    added_loc_cap: int = 500,
    branch: str | None = None,
) -> list[PRRecord]:
    repo_path = repo_path.expanduser().resolve()
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    branch = branch or _default_branch(repo_path)

    shas = _recent_shas(repo_path, branch, max_commits)
    selected: list[PRRecord] = []
    for sha in shas:
        meta = _commit_meta(repo_path, sha)
        if meta is None:
            continue
        if meta["added"] > added_loc_cap:
            continue
        if meta["added"] == 0 and meta["deleted"] == 0:
            continue  # skip merge / tag-only commits
        selected.append(
            PRRecord(
                number=_short_sha_to_int(sha),
                title=meta["subject"],
                body=meta["body"],
                base_ref=branch,
                head_sha=sha,
                base_sha=meta["parent"],
                merge_commit_sha=sha,
                language=language,
                added_lines=meta["added"],
                deleted_lines=meta["deleted"],
                changed_files=meta["files"],
                review_comments=[],
                repo_path=str(repo_path),
            )
        )
    out_path = CORPUS_DIR / f"{repo_path.name}.json"
    out_path.write_text(json.dumps([asdict(r) for r in selected], indent=2))
    return selected


def _default_branch(repo: Path) -> str:
    # Prefer the branch HEAD points at on the remote.
    res = subprocess.run(  # noqa: S603
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip().rsplit("/", 1)[-1]
    # Fall back to local HEAD branch name.
    res = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return (res.stdout.strip() or "main")


def _recent_shas(repo: Path, branch: str, limit: int) -> list[str]:
    res = subprocess.run(  # noqa: S603
        ["git", "log", "--no-merges", "-n", str(limit), "--format=%H", branch],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return [s for s in res.stdout.splitlines() if s.strip()]


def _commit_meta(repo: Path, sha: str) -> dict | None:
    # Subject + body
    show = subprocess.run(  # noqa: S603
        ["git", "show", "--no-patch", "--format=%H%n%P%n%s%n%b", sha],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if show.returncode != 0:
        return None
    lines = show.stdout.splitlines()
    if len(lines) < 3:
        return None
    parents = lines[1].split()
    if not parents:
        return None  # initial commit, no base to check out
    parent = parents[0]
    subject = lines[2]
    body = "\n".join(lines[3:]).strip()

    stat = subprocess.run(  # noqa: S603
        ["git", "show", "--numstat", "--format=", sha],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    added = deleted = 0
    files: list[str] = []
    for line in stat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, d, path = parts
        try:
            added += int(a)
            deleted += int(d)
        except ValueError:
            continue  # binary file: `-\t-\t<path>`
        files.append(path)

    return {
        "parent": parent,
        "subject": subject,
        "body": body or subject,
        "added": added,
        "deleted": deleted,
        "files": files,
    }


def _short_sha_to_int(sha: str) -> int:
    return int(sha[:7], 16)
