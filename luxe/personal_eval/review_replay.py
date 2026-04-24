"""B1 — review replay.

For each PR in the corpus:
1. Checkout the merge-base (pre-merge state).
2. Extract the diff that the PR introduced.
3. Ask the model to review it.
4. Parse the model's blocker/nit/suggestion lists.
5. Score against the human reviewer comments with `rubric.score_review`.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from harness import io
from harness.backends import Backend
from harness.metrics import RunMetrics
from personal_eval.gh_ingest import PRRecord, load_corpus
from personal_eval.rubric import score_review

_console = Console()

SYSTEM_PROMPT = """\
You are a rigorous code reviewer. You will receive a unified diff and a PR
description. Produce three sections, each as a JSON array of short strings:

{
  "blockers": [...],
  "nits": [...],
  "suggestions": [...]
}

Only return the JSON object. No prose before or after.
"""


@dataclass
class ReviewResult:
    pr_number: int
    language: str
    repo: str
    precision: float
    recall: float
    f1: float
    matched: int
    missed: int
    false_positives: int
    model_raw: str
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def review_pr(
    backend: Backend,
    pr: PRRecord,
    *,
    candidate_id: str,
    config_id: str,
) -> ReviewResult:
    repo = Path(pr.repo_path)
    diff = _diff_for_pr(repo, pr.base_sha, pr.head_sha)

    user = (
        f"PR #{pr.number}: {pr.title}\n\n"
        f"Description:\n{pr.body or '(no description)'}\n\n"
        f"Unified diff:\n```diff\n{diff[:20000]}\n```"
    )

    metrics = RunMetrics(
        candidate_id=candidate_id,
        config_id=config_id,
        benchmark="personal_review",
        task_id=f"{repo.name}#{pr.number}",
    )

    response = backend.chat(
        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
        max_tokens=1500,
        temperature=0.2,
    )
    metrics.record_turn(0, response)
    metrics.finish()

    model_comments = _parse_review(response.text)
    human_comments = [c["body"] for c in pr.review_comments if c.get("body")]
    if not human_comments:
        # No ground truth (commit-based ingestion, or a PR with no reviews).
        # Record the model's review verbatim; leave scoring metrics at 0 so
        # the report flags it as unscored. Useful for eyeballing quality.
        from personal_eval.rubric import ReviewScore

        rs = ReviewScore(
            precision=0.0, recall=0.0, f1=0.0,
            matched_issues=[], missed_issues=[], false_positives=model_comments,
        )
    else:
        rs = score_review(model_comments, human_comments)

    return ReviewResult(
        pr_number=pr.number,
        language=pr.language,
        repo=repo.name,
        precision=rs.precision,
        recall=rs.recall,
        f1=rs.f1,
        matched=len(rs.matched_issues),
        missed=len(rs.missed_issues),
        false_positives=len(rs.false_positives),
        model_raw=response.text[:4000],
        metrics=metrics.to_dict(),
    )


def replay_corpus(
    backend: Backend,
    repo_name: str,
    *,
    candidate_id: str,
    config_id: str,
    limit: int | None = None,
) -> list[ReviewResult]:
    corpus = load_corpus(repo_name)
    if limit:
        corpus = corpus[:limit]
    out_path = io.runs_path("phase_b", candidate_id, config_id, f"review_{repo_name}")
    done = io.completed_task_ids(out_path)

    results: list[ReviewResult] = []
    remaining = [pr for pr in corpus if f"{repo_name}#{pr.number}" not in done]
    for i, pr in enumerate(remaining, 1):
        _console.log(f"[B1 review] {repo_name} · {i}/{len(remaining)} · PR/commit #{pr.number}")
        tid = f"{repo_name}#{pr.number}"
        try:
            res = review_pr(backend, pr, candidate_id=candidate_id, config_id=config_id)
        except Exception as e:  # noqa: BLE001
            _console.log(f"  [error] {type(e).__name__}: {e}")
            continue
        io.append(out_path, {"task_id": tid, **res.to_dict()})
        results.append(res)
    return results


def _diff_for_pr(repo: Path, base_sha: str, head_sha: str) -> str:
    res = subprocess.run(  # noqa: S603
        ["git", "diff", f"{base_sha}..{head_sha}"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return res.stdout


def _parse_review(text: str) -> list[str]:
    # Pull the first JSON object out of the response; concatenate all three sections.
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return []
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    out: list[str] = []
    for key in ("blockers", "nits", "suggestions"):
        for item in obj.get(key, []) or []:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
    return out
