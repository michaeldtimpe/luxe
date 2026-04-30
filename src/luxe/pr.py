"""End-to-end PR cycle — branch → commit → test → push → PR create → CI watch.

Per plan §5: each step is checkpointed in pr_state.json so a partial failure
(e.g. `gh auth` expired between push and PR-create) can be resumed via
`luxe pr <run-id>` rather than restarting the whole pipeline.

Public API:
- preflight_repo()  — run BEFORE the pipeline; checks gh auth, dirty tree.
- detect_test_command() — best-effort match against repo marker files.
- plan_branch_name() — slug + collision suffix.
- open_pr() — runs the full post-pipeline PR cycle.
- resume_pr() — picks up at the first incomplete step.

Empty-diff handling is task-type-aware:
- review/summarize: empty diff is expected; status `done_no_changes`.
- implement/bugfix/document/manage: empty diff is a failure
  (`failed_no_mutations_produced`) — no PR opened, run flagged.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from luxe.run_state import (
    PRState,
    PRStep,
    RunSpec,
    append_event,
    load_pr_state,
    load_run_spec,
    run_dir,
    save_pr_state,
)


_WRITE_TASK_TYPES = {"implement", "bugfix", "document", "manage"}
_READ_ONLY_TASK_TYPES = {"review", "summarize"}


# --- exceptions ------------------------------------------------------------

class PRError(RuntimeError):
    """Base class for PR-cycle failures."""


class GhAuthError(PRError):
    """gh CLI is missing or the user is not authenticated."""


class DirtyTreeError(PRError):
    """Working tree has uncommitted changes and --allow-dirty was not given."""


class NoMutationsError(PRError):
    """Write task produced no diff. Status: failed_no_mutations_produced."""


# --- config ----------------------------------------------------------------

@dataclass
class PRConfig:
    test_commands: list[dict[str, Any]]
    watch_ci_enabled: bool = False
    watch_ci_poll_interval_s: int = 30
    watch_ci_total_wait_s: int = 300
    convert_to_ready_on_green: bool = True
    dirty_tree: str = "abort"
    branch_prefix: str = "luxe"
    draft_on_test_failure: bool = True
    test_output_tail_lines: int = 200


def default_pr_config_path() -> Path:
    return Path(__file__).parent.parent.parent / "configs" / "pr.yaml"


def load_pr_config(path: str | Path | None = None) -> PRConfig:
    p = Path(path) if path else default_pr_config_path()
    raw: dict[str, Any] = yaml.safe_load(p.read_text())
    watch = raw.get("watch_ci") or {}
    return PRConfig(
        test_commands=raw.get("test_commands", []),
        watch_ci_enabled=bool(watch.get("enabled", False)),
        watch_ci_poll_interval_s=int(watch.get("poll_interval_s", 30)),
        watch_ci_total_wait_s=int(watch.get("total_wait_s", 300)),
        convert_to_ready_on_green=bool(watch.get("convert_to_ready_on_green", True)),
        dirty_tree=str(raw.get("dirty_tree", "abort")),
        branch_prefix=str(raw.get("branch_prefix", "luxe")),
        draft_on_test_failure=bool(raw.get("draft_on_test_failure", True)),
        test_output_tail_lines=int(raw.get("test_output_tail_lines", 200)),
    )


# --- subprocess helpers ----------------------------------------------------

@dataclass
class CmdResult:
    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def combined_tail(self, lines: int = 200) -> str:
        text = (self.stdout + ("\n" + self.stderr if self.stderr else "")).rstrip()
        if not text:
            return ""
        ls = text.splitlines()
        if len(ls) <= lines:
            return text
        return "\n".join(ls[-lines:])


def _run(cmd: list[str], cwd: str | Path, env: dict | None = None,
         timeout: float | None = None) -> CmdResult:
    proc = subprocess.run(
        cmd, cwd=str(cwd), env=env, capture_output=True, text=True,
        check=False, timeout=timeout,
    )
    return CmdResult(rc=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


# --- preflight -------------------------------------------------------------

def assert_gh_auth() -> None:
    """Raise GhAuthError if `gh` is missing or not authenticated."""
    try:
        result = _run(["gh", "auth", "status"], cwd=Path.cwd())
    except FileNotFoundError as e:
        raise GhAuthError(
            "GitHub CLI (`gh`) not found. Install with `brew install gh` and "
            "authenticate with `gh auth login`."
        ) from e
    if not result.ok:
        raise GhAuthError(
            "GitHub CLI is not authenticated. Run `gh auth login` and re-run."
        )


def is_dirty(repo_path: str | Path) -> bool:
    r = _run(["git", "status", "--porcelain"], cwd=repo_path)
    return r.ok and bool(r.stdout.strip())


def assert_clean_tree(repo_path: str | Path, *,
                      allow_dirty: bool,
                      confirm_callback: Callable[[], bool] | None = None) -> None:
    """Raise DirtyTreeError if the tree is dirty and not explicitly allowed.

    `confirm_callback` is invoked when allow_dirty=True; if it returns False
    we still abort (TTY user typed something other than 'yes'). For scripts,
    pass --yes (the CLI maps that to confirm_callback that returns True).
    """
    if not is_dirty(repo_path):
        return
    if not allow_dirty:
        raise DirtyTreeError(
            "luxe refuses to start with uncommitted changes — commit, stash, "
            "or pass `--allow-dirty` to proceed (the PR diff will include them)."
        )
    if confirm_callback is None:
        # No confirm channel; --allow-dirty without --yes on a non-TTY shell
        # is an error of omission, treat as not-confirmed.
        raise DirtyTreeError(
            "--allow-dirty requires explicit confirmation. Re-run on a TTY "
            "(typed 'yes' confirmation) or pass --yes for non-interactive use."
        )
    if not confirm_callback():
        raise DirtyTreeError("--allow-dirty was not confirmed by the user.")


def detect_base_branch(repo_path: str | Path) -> str:
    r = _run(["gh", "repo", "view", "--json", "defaultBranch", "-q", ".defaultBranch"],
             cwd=repo_path)
    if r.ok and r.stdout.strip():
        return r.stdout.strip()
    # Fallback: parse `git symbolic-ref refs/remotes/origin/HEAD`
    r = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo_path)
    if r.ok and r.stdout.strip():
        return r.stdout.strip().rsplit("/", 1)[-1]
    return "main"


def head_sha(repo_path: str | Path) -> str:
    r = _run(["git", "rev-parse", "HEAD"], cwd=repo_path)
    return r.stdout.strip() if r.ok else ""


# --- test detection --------------------------------------------------------

def detect_test_command(repo_path: str | Path, cfg: PRConfig) -> str:
    repo = Path(repo_path)
    for entry in cfg.test_commands:
        markers = entry.get("markers", [])
        for m in markers:
            if (repo / m).exists():
                return str(entry.get("command", ""))
    return ""


# --- branch naming ---------------------------------------------------------

def slugify_goal(goal: str, max_words: int = 6) -> str:
    words = re.findall(r"[a-zA-Z0-9]+", goal.lower())
    slug = "-".join(words[:max_words])
    return slug or "goal"


def _branch_exists_local(repo_path: Path, name: str) -> bool:
    r = _run(["git", "rev-parse", "--verify", f"refs/heads/{name}"], cwd=repo_path)
    return r.ok


def _branch_exists_remote(repo_path: Path, name: str) -> bool:
    r = _run(["git", "ls-remote", "--exit-code", "--heads", "origin", name], cwd=repo_path)
    return r.ok


def plan_branch_name(task_type: str, goal: str, repo_path: str | Path,
                     cfg: PRConfig) -> str:
    repo = Path(repo_path)
    base = f"{cfg.branch_prefix}/{task_type}/{slugify_goal(goal)}"
    candidate = base
    n = 2
    while _branch_exists_local(repo, candidate) or _branch_exists_remote(repo, candidate):
        candidate = f"{base}-{n}"
        n += 1
        if n > 99:
            raise PRError(f"Cannot find a free branch name based on `{base}`")
    return candidate


# --- main flow -------------------------------------------------------------

@dataclass
class PRPreflight:
    base_branch: str
    base_sha: str
    branch_name: str
    test_command: str


def preflight(
    repo_path: str | Path,
    *,
    task_type: str,
    goal: str,
    allow_dirty: bool = False,
    confirm_callback: Callable[[], bool] | None = None,
    cfg: PRConfig | None = None,
) -> PRPreflight:
    """All the checks that must pass BEFORE the pipeline begins.

    Run this before launching any model-load / model-call work so we don't
    discover a missing `gh` or a dirty tree mid-run after burning compute.
    """
    cfg = cfg or load_pr_config()
    assert_gh_auth()
    assert_clean_tree(repo_path, allow_dirty=allow_dirty, confirm_callback=confirm_callback)
    return PRPreflight(
        base_branch=detect_base_branch(repo_path),
        base_sha=head_sha(repo_path),
        branch_name=plan_branch_name(task_type, goal, repo_path, cfg),
        test_command=detect_test_command(repo_path, cfg),
    )


# --- step implementations --------------------------------------------------

def _short_subject(task_type: str, goal: str, max_len: int = 70) -> str:
    g = goal.strip()
    if len(g) > max_len - len(task_type) - 2:
        g = g[: max_len - len(task_type) - 5].rstrip() + "..."
    return f"{task_type}: {g}"


def _commit_body_excerpt(report_text: str, max_chars: int = 1200) -> str:
    text = (report_text or "").strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n…(truncated; full report attached to PR body)"


def _do_commit(spec: RunSpec, state: PRState, report_text: str,
               task_type: str, goal: str) -> None:
    repo = Path(spec.repo_path)
    step = state.step("commit")
    if step.done:
        return

    diff = _run(["git", "status", "--porcelain"], cwd=repo)
    if not diff.stdout.strip():
        if task_type in _WRITE_TASK_TYPES:
            step.status = "failed"
            step.detail = "no diff produced (failed_no_mutations_produced)"
            raise NoMutationsError(
                f"task_type={task_type} produced no diff — workers did not "
                "write or edit any files. Check the synthesizer report for "
                "execution failures."
            )
        step.status = "skipped"
        step.detail = "no diff (read-only task)"
        step.done = True
        step.completed_at = time.time()
        return

    # Create branch
    co = _run(["git", "checkout", "-b", state.branch_name], cwd=repo)
    if not co.ok:
        step.status = "failed"
        step.detail = f"git checkout failed: {co.stderr.strip()[:300]}"
        raise PRError(step.detail)

    add = _run(["git", "add", "-A"], cwd=repo)
    if not add.ok:
        step.status = "failed"
        step.detail = f"git add failed: {add.stderr.strip()[:300]}"
        raise PRError(step.detail)

    subject = _short_subject(task_type, goal)
    body = _commit_body_excerpt(report_text)
    cm = _run(["git", "commit", "-m", subject, "-m", body], cwd=repo)
    if not cm.ok:
        step.status = "failed"
        step.detail = f"git commit failed: {cm.stderr.strip()[:300]}"
        raise PRError(step.detail)

    step.done = True
    step.status = "done"
    step.completed_at = time.time()


def _do_test(spec: RunSpec, state: PRState, cfg: PRConfig) -> None:
    repo = Path(spec.repo_path)
    step = state.step("test")
    if step.done:
        return
    cmd_str = state.test_command
    if not cmd_str:
        step.status = "skipped"
        step.detail = "no test command detected"
        step.done = True
        step.completed_at = time.time()
        state.test_passed = None
        return
    # Honour user shell quoting for the test command (it's a string from yaml).
    res = _run(["bash", "-lc", cmd_str], cwd=repo)
    state.test_passed = res.ok
    state.test_output_tail = res.combined_tail(cfg.test_output_tail_lines)
    step.done = True
    step.status = "done"
    step.detail = f"rc={res.rc}"
    step.completed_at = time.time()


def _do_push(spec: RunSpec, state: PRState) -> None:
    repo = Path(spec.repo_path)
    step = state.step("push")
    if step.done:
        return
    res = _run(["git", "push", "-u", "origin", state.branch_name], cwd=repo)
    if not res.ok:
        step.status = "failed"
        step.detail = f"git push failed: {res.stderr.strip()[:500]}"
        raise PRError(step.detail)
    step.done = True
    step.status = "done"
    step.completed_at = time.time()


def _format_pr_body(spec: RunSpec, state: PRState, report_text: str,
                    task_type: str) -> str:
    sections: list[str] = []
    sections.append(report_text.strip() if report_text else "")
    sections.append("---")
    sections.append("")
    sections.append("## Run details")
    sections.append(f"- run_id: `{spec.run_id}`")
    sections.append(f"- task_type: `{task_type}`")
    sections.append(f"- mode: `mono`")
    sections.append(f"- base_sha: `{spec.base_sha[:12]}`")
    if state.test_command:
        verdict = "✓ pass" if state.test_passed else "✗ fail"
        sections.append(f"- tests (`{state.test_command}`): {verdict}")
        if state.test_output_tail:
            sections.append("")
            sections.append("<details><summary>Test output (tail)</summary>")
            sections.append("")
            sections.append("```")
            sections.append(state.test_output_tail)
            sections.append("```")
            sections.append("</details>")
    sections.append("")
    sections.append("_Generated by luxe v1.0._")
    return "\n".join(sections)


def _do_create(spec: RunSpec, state: PRState, report_text: str,
               task_type: str, goal: str, cfg: PRConfig) -> None:
    repo = Path(spec.repo_path)
    step = state.step("create")
    if step.done:
        return

    title = _short_subject(task_type, goal)
    body = _format_pr_body(spec, state, report_text, task_type)
    is_draft = bool(state.test_passed is False and cfg.draft_on_test_failure)
    state.is_draft = is_draft
    args = ["gh", "pr", "create", "--title", title, "--body", body,
            "--base", spec.base_branch, "--head", state.branch_name]
    if is_draft:
        args.append("--draft")
    res = _run(args, cwd=repo)
    if not res.ok:
        step.status = "failed"
        step.detail = f"gh pr create failed: {res.stderr.strip()[:500]}"
        raise PRError(step.detail)

    # Capture PR URL + number from gh stdout.
    url = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
    state.pr_url = url
    m = re.search(r"/pull/(\d+)", url)
    if m:
        state.pr_number = int(m.group(1))
    step.done = True
    step.status = "done"
    step.detail = url
    step.completed_at = time.time()


def _do_watch_ci(spec: RunSpec, state: PRState, cfg: PRConfig) -> None:
    repo = Path(spec.repo_path)
    step = state.step("watch_ci")
    if step.done:
        return
    if not state.pr_number:
        step.status = "skipped"
        step.detail = "no PR number"
        step.done = True
        step.completed_at = time.time()
        return

    deadline = time.monotonic() + cfg.watch_ci_total_wait_s
    final_state = "timeout"
    failing_check = ""
    while time.monotonic() < deadline:
        res = _run(["gh", "pr", "checks", str(state.pr_number)], cwd=repo)
        out = res.stdout
        # gh's `pr checks` returns 0 on green, 8 on pending, non-zero on red
        # depending on version. We grep the output rather than relying on rc.
        if "fail" in out.lower():
            final_state = "failed"
            for line in out.splitlines():
                if "fail" in line.lower():
                    failing_check = line.strip()
                    break
            break
        if res.ok and "pass" in out.lower() and "pending" not in out.lower():
            final_state = "passed"
            break
        time.sleep(cfg.watch_ci_poll_interval_s)

    if final_state == "passed":
        if cfg.convert_to_ready_on_green and state.is_draft:
            _run(["gh", "pr", "ready", str(state.pr_number)], cwd=repo)
            state.is_draft = False
        step.detail = "ci passed"
    elif final_state == "failed":
        # If we opened it ready, convert back to draft
        if not state.is_draft:
            _run(["gh", "pr", "ready", str(state.pr_number), "--undo"], cwd=repo)
            state.is_draft = True
        step.detail = f"ci failed: {failing_check}"
    else:
        step.detail = "watch timed out"

    step.done = True
    step.status = "done"
    step.completed_at = time.time()


# --- public entry points ---------------------------------------------------

def open_pr(
    spec: RunSpec,
    *,
    report_text: str,
    task_type: str,
    goal: str,
    test_command: str,
    branch_name: str,
    cfg: PRConfig | None = None,
    watch_ci: bool = False,
    on_event: Callable[[str, dict], None] | None = None,
) -> PRState:
    """Run the full post-pipeline PR cycle. Each step writes pr_state.json
    so a partial failure can be resumed via `luxe pr <run-id>`.

    May raise NoMutationsError when task_type ∈ {implement, bugfix, document,
    manage} and the worker pipeline produced no diff. Other PRErrors propagate
    after the failed step is recorded.
    """
    cfg = cfg or load_pr_config()
    state = load_pr_state(spec.run_id) or PRState()
    state.branch_name = state.branch_name or branch_name
    state.test_command = state.test_command or test_command

    def _emit(kind: str, **data) -> None:
        append_event(spec.run_id, kind, **data)
        if on_event:
            on_event(kind, data)

    save_pr_state(spec.run_id, state)
    try:
        if not state.is_done("commit"):
            _emit("pr_step_begin", step="commit")
            _do_commit(spec, state, report_text, task_type, goal)
            save_pr_state(spec.run_id, state)
            _emit("pr_step_end", step="commit", status=state.step("commit").status)
            if state.step("commit").status == "skipped":
                # Read-only task; no PR.
                return state

        if not state.is_done("test"):
            _emit("pr_step_begin", step="test")
            _do_test(spec, state, cfg)
            save_pr_state(spec.run_id, state)
            _emit("pr_step_end", step="test", status=state.step("test").status,
                  test_passed=state.test_passed)

        if not state.is_done("push"):
            _emit("pr_step_begin", step="push")
            _do_push(spec, state)
            save_pr_state(spec.run_id, state)
            _emit("pr_step_end", step="push", status=state.step("push").status)

        if not state.is_done("create"):
            _emit("pr_step_begin", step="create")
            _do_create(spec, state, report_text, task_type, goal, cfg)
            save_pr_state(spec.run_id, state)
            _emit("pr_step_end", step="create",
                  status=state.step("create").status,
                  pr_url=state.pr_url, pr_number=state.pr_number)

        if watch_ci and not state.is_done("watch_ci"):
            _emit("pr_step_begin", step="watch_ci")
            _do_watch_ci(spec, state, cfg)
            save_pr_state(spec.run_id, state)
            _emit("pr_step_end", step="watch_ci",
                  status=state.step("watch_ci").status,
                  detail=state.step("watch_ci").detail)
    except PRError as e:
        save_pr_state(spec.run_id, state)
        _emit("pr_blocked", error=str(e))
        raise
    except Exception as e:
        save_pr_state(spec.run_id, state)
        _emit("pr_unexpected_error", error=f"{type(e).__name__}: {e}")
        raise

    return state


def resume_pr(run_id: str, *, push_only: bool = False, watch_ci: bool = False,
              on_event: Callable[[str, dict], None] | None = None) -> PRState:
    """Resume a partially-completed PR cycle from its last incomplete step.

    Loads RunSpec + PRState from ~/.luxe/runs/<run_id>/. Re-loads the report
    from the synthesizer artefact if present. `push_only` stops after push
    so the user can craft the PR description manually.
    """
    spec = load_run_spec(run_id)
    if spec is None:
        raise PRError(f"unknown run_id {run_id}")
    state = load_pr_state(run_id) or PRState()
    if not state.branch_name:
        raise PRError(f"run {run_id} has no branch_name in pr_state.json")

    cfg = load_pr_config()
    report_path = run_dir(run_id) / "synthesizer.md"
    report_text = report_path.read_text() if report_path.is_file() else ""

    def _emit(kind: str, **data) -> None:
        append_event(run_id, kind, **data)
        if on_event:
            on_event(kind, data)

    _emit("pr_resume_begin", from_step=_first_incomplete(state),
          push_only=push_only, watch_ci=watch_ci)

    try:
        if not state.is_done("test"):
            _do_test(spec, state, cfg)
            save_pr_state(run_id, state)
        if not state.is_done("push"):
            _do_push(spec, state)
            save_pr_state(run_id, state)
        if push_only:
            return state
        if not state.is_done("create"):
            _do_create(spec, state, report_text, spec.task_type, spec.goal, cfg)
            save_pr_state(run_id, state)
        if watch_ci and not state.is_done("watch_ci"):
            _do_watch_ci(spec, state, cfg)
            save_pr_state(run_id, state)
    except PRError:
        save_pr_state(run_id, state)
        raise

    return state


def _first_incomplete(state: PRState) -> str:
    for name in ("commit", "test", "push", "create", "watch_ci"):
        s = state.step_or_none(name)
        if s is None or not s.done:
            return name
    return "complete"
