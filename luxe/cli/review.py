"""Headless helpers for the review / refactor flows.

The REPL's `/review` and `/refactor` commands and the CLI's
`luxe analyze --review` share the same goal string and task plumbing.
The REPL adds interactive plan confirmation on top; the CLI path skips
straight to background spawn.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cli.git import repo_name_from_url, resolve_repo
from cli.registry import LuxeConfig
from cli.repo_survey import BudgetDecision, RepoSurvey, analyze_repo, size_budgets
from cli.tasks import plan
from cli.tasks.model import Task, persist, task_id


def size_review_budget(repo_path: Path) -> BudgetDecision:
    """Pre-flight survey → budget decision for a /review or /refactor
    task. Single source of truth shared by the interactive REPL path
    and the headless `luxe analyze --review` path, so both land on
    the same tier for the same repo."""
    return size_budgets(analyze_repo(repo_path))


def survey_and_budget(repo_path: Path) -> tuple[RepoSurvey, BudgetDecision]:
    """Like size_review_budget but also returns the underlying survey so
    callers can pass `language_breakdown` through to analyzer gating."""
    survey = analyze_repo(repo_path)
    return survey, size_budgets(survey)


def build_review_goal(repo_label: str, repo_path: Path, mode: str) -> str:
    """Single source of truth for the review/refactor goal prompt.

    Keep in sync with repl._start_review's goal strings — both call into
    this helper."""
    if mode == "review":
        return (
            f"Review the `{repo_label}` repository at {repo_path!s}. "
            f"Start all file reads relative to this path. "
            f"Start by listing the root with `list_dir` and reading any "
            f"README/ARCHITECTURE/CONTRIBUTING/SECURITY/docs files. Then "
            f"systematically look for: (1) security issues — input handling, "
            f"auth, secrets, injection, deserialization, path traversal, "
            f"dependency vulns; (2) correctness bugs — error handling, race "
            f"conditions, resource leaks, silent failures; (3) robustness — "
            f"missing timeouts/retries, unbounded loops; (4) maintainability "
            f"issues that mask real risk. End with a severity-grouped "
            f"markdown report. Ground every finding in code you read via "
            f"tools — no invented filenames or quotes."
        )
    return (
        f"Analyze the `{repo_label}` repository at {repo_path!s} for "
        f"optimization and refactor opportunities. "
        f"Start all file reads relative to this path. "
        f"Start by listing the root with `list_dir` and reading the README "
        f"and core entry points. Then systematically identify: (1) "
        f"performance — obvious algorithmic inefficiency, missing caching, "
        f"unbatched I/O; (2) architectural issues — leaky "
        f"abstractions, modules that should split or merge, painful "
        f"API surfaces; (3) code-size wins — duplication, dead code; "
        f"(4) idiomatic improvements that cut real complexity. End "
        f"with an impact-ranked markdown report of recommended changes. "
        f"Ground every suggestion in code you actually read."
    )


def start_review_task(
    url_or_path: str | Path,
    mode: str,
    cfg: LuxeConfig,
) -> str:
    """Plan + persist + spawn a review/refactor task. Returns task id.

    Headless — no ReplState needed. Used by `luxe analyze --review`."""
    repo_path, status_msg = resolve_repo(str(url_or_path), Path.cwd())
    if repo_path is None:
        raise RuntimeError(status_msg)

    repo_label = repo_name_from_url(str(url_or_path)) or repo_path.name
    goal = build_review_goal(repo_label, repo_path, mode)

    # Pre-flight repo survey sizes the task wall + num_ctx so tiny
    # repos don't waste budget and large repos aren't starved of it.
    survey, decision = survey_and_budget(repo_path)
    task = Task(
        id=task_id(),
        goal=goal,
        max_wall_s=decision.task_max_wall_s,
        num_ctx_override=decision.num_ctx,
        analyzer_languages=sorted(survey.language_breakdown.keys()) or None,
    )
    task.subtasks = plan(goal, cfg, task.id)
    for s in task.subtasks:
        s.agent = mode
    persist(task)

    log_path = task.dir() / "stdout.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    f = log_path.open("ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "cli.tasks.run", task.id],
        stdout=f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(repo_path),
    )
    task.pid = proc.pid
    persist(task)
    (task.dir() / "repo_path").write_text(str(repo_path))
    return task.id
