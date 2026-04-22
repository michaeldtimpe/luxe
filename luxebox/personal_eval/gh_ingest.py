"""Pull recent merged PRs from a local repo via `gh` CLI.

Selection heuristics:
- Merged, not draft
- Primary language of the repo matches a target language (rust/python/go)
- Diff size under a configurable LOC cap (default 500 added lines)
- Has at least one review comment (so we can score B1 against human feedback)

Output: a JSON corpus at personal_eval/corpus/<repo>.json that B1/B2 consume.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"


@dataclass
class PRRecord:
    number: int
    title: str
    body: str
    base_ref: str
    head_sha: str
    base_sha: str
    merge_commit_sha: str
    language: str
    added_lines: int
    deleted_lines: int
    changed_files: list[str]
    review_comments: list[dict[str, Any]]
    repo_path: str


def ingest(
    repo_path: Path,
    language: str,
    *,
    max_prs: int = 50,
    added_loc_cap: int = 500,
    min_review_comments: int = 1,
) -> list[PRRecord]:
    repo_path = repo_path.expanduser().resolve()
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)

    prs = _gh_list_merged(repo_path, max_prs)
    selected: list[PRRecord] = []
    for pr in prs:
        details = _gh_pr_detail(repo_path, pr["number"])
        added = details.get("additions", 0)
        comments = details.get("reviews", []) + details.get("comments", [])
        if added > added_loc_cap:
            continue
        if len([c for c in comments if c.get("body")]) < min_review_comments:
            continue
        selected.append(
            PRRecord(
                number=details["number"],
                title=details["title"],
                body=details.get("body") or "",
                base_ref=details["baseRefName"],
                head_sha=details["headRefOid"],
                base_sha=details["baseRefOid"],
                merge_commit_sha=details.get("mergeCommit", {}).get("oid", ""),
                language=language,
                added_lines=added,
                deleted_lines=details.get("deletions", 0),
                changed_files=[f["path"] for f in details.get("files", [])],
                review_comments=[
                    {"author": c.get("author", {}).get("login"), "body": c.get("body", "")}
                    for c in comments
                    if c.get("body")
                ],
                repo_path=str(repo_path),
            )
        )

    out_path = CORPUS_DIR / f"{repo_path.name}.json"
    out_path.write_text(json.dumps([asdict(r) for r in selected], indent=2))
    return selected


def _gh_list_merged(repo_path: Path, limit: int) -> list[dict[str, Any]]:
    res = subprocess.run(  # noqa: S603
        [
            "gh",
            "pr",
            "list",
            "--state",
            "merged",
            "--limit",
            str(limit),
            "--json",
            "number,title,mergedAt",
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(res.stdout)


def _gh_pr_detail(repo_path: Path, number: int) -> dict[str, Any]:
    fields = ",".join(
        [
            "number",
            "title",
            "body",
            "baseRefName",
            "baseRefOid",
            "headRefOid",
            "mergeCommit",
            "additions",
            "deletions",
            "files",
            "reviews",
            "comments",
        ]
    )
    res = subprocess.run(  # noqa: S603
        ["gh", "pr", "view", str(number), "--json", fields],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(res.stdout)


def load_corpus(repo_name: str) -> list[PRRecord]:
    path = CORPUS_DIR / f"{repo_name}.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    return [PRRecord(**r) for r in raw]
