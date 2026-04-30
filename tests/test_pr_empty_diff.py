"""Empty-diff handling — task-type-aware.

Per plan §5: write tasks (implement/bugfix/document/manage) with an empty diff
must fail with NoMutationsError so silent worker failures aren't masked as
success. Read-only tasks (review/summarize) with empty diff are expected.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe import pr as pr_mod
from luxe.pr import (
    CmdResult,
    NoMutationsError,
    PRConfig,
    PRState,
)
from luxe.run_state import RunSpec


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("# repo\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _spec(repo: Path, task_type: str, run_id: str = "test01") -> RunSpec:
    return RunSpec(
        run_id=run_id, goal="g", task_type=task_type,
        repo_path=str(repo), base_sha="", base_branch="main",
    )


@pytest.mark.parametrize("task_type", ["implement", "bugfix", "document", "manage"])
def test_empty_diff_write_task_raises(git_repo: Path, task_type: str):
    state = PRState(branch_name=f"luxe/{task_type}/x")
    spec = _spec(git_repo, task_type)
    with pytest.raises(NoMutationsError):
        pr_mod._do_commit(spec, state, report_text="report", task_type=task_type, goal="g")
    step = state.step_or_none("commit")
    assert step is not None
    assert step.status == "failed"
    assert "no diff" in step.detail


@pytest.mark.parametrize("task_type", ["review", "summarize"])
def test_empty_diff_read_only_skips_cleanly(git_repo: Path, task_type: str):
    state = PRState(branch_name=f"luxe/{task_type}/x")
    spec = _spec(git_repo, task_type)
    pr_mod._do_commit(spec, state, report_text="report", task_type=task_type, goal="g")
    step = state.step("commit")
    assert step.done
    assert step.status == "skipped"


def test_non_empty_diff_commits_branch(git_repo: Path, monkeypatch, tmp_path):
    # Stage a real change so git diff is non-empty.
    (git_repo / "new.txt").write_text("hello")

    # Isolate run dir
    monkeypatch.setattr("luxe.run_state.runs_root",
                        lambda: tmp_path / "luxe-runs")

    state = PRState(branch_name="luxe/bugfix/fix-x")
    spec = _spec(git_repo, "bugfix")
    pr_mod._do_commit(spec, state, report_text="report body",
                      task_type="bugfix", goal="fix x")
    step = state.step("commit")
    assert step.done
    assert step.status == "done"
    # Verify branch is checked out and commit landed.
    cur = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                         cwd=git_repo, capture_output=True, text=True, check=True)
    assert cur.stdout.strip() == "luxe/bugfix/fix-x"
    log = subprocess.run(["git", "log", "--oneline", "-1"],
                         cwd=git_repo, capture_output=True, text=True, check=True)
    assert "bugfix: fix x" in log.stdout
