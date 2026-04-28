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

from luxe_cli.git import repo_name_from_url, resolve_repo
from luxe_cli.registry import LuxeConfig
from luxe_cli.repo_survey import BudgetDecision, RepoSurvey, analyze_repo, size_budgets
from luxe_cli.tasks import plan
from luxe_cli.tasks.model import Task, persist, task_id


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
    this helper.

    Note on the orientation step: earlier wording asked the agent to
    "read any README/ARCHITECTURE/CONTRIBUTING/SECURITY/docs files",
    which the planner faithfully turned into a sub 02 like "Read the
    README, ARCHITECTURE, CONTRIBUTING, SECURITY, and docs files."
    Agents then probed each filename one by one — three usually don't
    exist in JS/TS repos — and walked any `docs/` tree sequentially.
    A neon-rain run hit 28 minutes and exhausted the wall budget on
    sub 02 alone. New phrasing scopes the orientation to one
    `list_dir` plus the README only; later subtasks read what they
    actually need. Saves ~20 min on a 11k-LOC repo.
    """
    if mode == "review":
        return (
            f"Review the `{repo_label}` repository at {repo_path!s}. "
            f"Start all file reads relative to this path. "
            f"Orient with one `list_dir` of the root, then read the "
            f"README only — do NOT probe ARCHITECTURE / CONTRIBUTING / "
            f"SECURITY / docs/ unless `list_dir` shows they exist, and "
            f"then prefer reading only the most relevant 1–2. Inspection "
            f"subtasks below read what they need. "
            f"Then systematically look for: (1) security issues — input "
            f"handling, auth, secrets, injection, deserialization, path "
            f"traversal, dependency vulns; (2) correctness bugs — error "
            f"handling, race conditions, resource leaks, silent failures; "
            f"(3) robustness — missing timeouts/retries, unbounded loops; "
            f"(4) maintainability issues that mask real risk. End with a "
            f"severity-grouped markdown report. Ground every finding in "
            f"code you read via tools — no invented filenames or quotes."
        )
    return (
        f"Analyze the `{repo_label}` repository at {repo_path!s} for "
        f"optimization and refactor opportunities. "
        f"Start all file reads relative to this path. "
        f"Orient with one `list_dir` of the root, then read the README "
        f"only. Skip ARCHITECTURE / CONTRIBUTING / SECURITY / docs/ "
        f"unless `list_dir` shows them; analysis subtasks read what they "
        f"need. Then systematically identify: (1) performance — obvious "
        f"algorithmic inefficiency, missing caching, unbatched I/O; (2) "
        f"architectural issues — leaky abstractions, modules that should "
        f"split or merge, painful API surfaces; (3) code-size wins — "
        f"duplication, dead code; (4) idiomatic improvements that cut "
        f"real complexity. End with an impact-ranked markdown report of "
        f"recommended changes. Ground every suggestion in code you "
        f"actually read."
    )


def start_review_task(
    url_or_path: str | Path,
    mode: str,
    cfg: LuxeConfig,
    *,
    use_plan_cache: bool = True,
) -> str:
    """Plan + persist + spawn a review/refactor task. Returns task id.

    Headless — no ReplState needed. Used by `luxe analyze --review`."""
    repo_path, status_msg = resolve_repo(str(url_or_path), cfg.cache_dir())
    if repo_path is None:
        raise RuntimeError(status_msg)

    repo_label = repo_name_from_url(str(url_or_path)) or repo_path.name
    goal = build_review_goal(repo_label, repo_path, mode)

    # Pre-flight repo survey sizes the task wall so tiny repos don't
    # waste budget and large repos aren't starved of it. ctx is fixed
    # per-agent in configs/agents.yaml.
    survey, decision = survey_and_budget(repo_path)
    task = Task(
        id=task_id(),
        goal=goal,
        max_wall_s=decision.task_max_wall_s,
        analyzer_languages=sorted(survey.language_breakdown.keys()) or None,
    )
    task.subtasks = plan(
        goal, cfg, task.id,
        cache_key=(str(repo_path), mode),
        use_cache=use_plan_cache,
    )
    for s in task.subtasks:
        s.agent = mode
    persist(task)

    log_path = task.dir() / "stdout.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    f = log_path.open("ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "luxe_cli.tasks.run", task.id],
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
