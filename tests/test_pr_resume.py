"""Resume semantics for the PR cycle — checkpoint-aware step restart."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe import pr as pr_mod
from luxe.pr import CmdResult, PRConfig, PRError, PRState, _first_incomplete
from luxe.run_state import (
    RunSpec,
    init_run_dir,
    load_pr_state,
    run_dir,
    save_pr_state,
)


@pytest.fixture(autouse=True)
def _isolate_runs_root(tmp_path, monkeypatch):
    monkeypatch.setattr("luxe.run_state.runs_root", lambda: tmp_path / "runs")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("# r\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _spec(repo: Path) -> RunSpec:
    return RunSpec(
        run_id="resume01", goal="fix x", task_type="bugfix",
        repo_path=str(repo), base_sha="", base_branch="main",
        actual_mode="single",
    )


def test_first_incomplete_with_no_state():
    state = PRState()
    assert _first_incomplete(state) == "commit"


def test_first_incomplete_after_commit():
    state = PRState()
    state.step("commit").done = True
    assert _first_incomplete(state) == "test"


def test_first_incomplete_after_push():
    state = PRState()
    for s in ("commit", "test", "push"):
        state.step(s).done = True
    assert _first_incomplete(state) == "create"


def test_first_incomplete_all_done():
    state = PRState()
    for s in ("commit", "test", "push", "create", "watch_ci"):
        state.step(s).done = True
    assert _first_incomplete(state) == "complete"


def test_save_and_load_pr_state(git_repo: Path):
    spec = _spec(git_repo)
    init_run_dir(spec)
    state = PRState(branch_name="luxe/bugfix/foo", pr_url="https://...", pr_number=42)
    state.step("commit").done = True
    save_pr_state(spec.run_id, state)

    loaded = load_pr_state(spec.run_id)
    assert loaded is not None
    assert loaded.branch_name == "luxe/bugfix/foo"
    assert loaded.pr_number == 42
    assert loaded.is_done("commit")


def test_resume_with_unknown_run_id():
    with pytest.raises(PRError) as exc:
        pr_mod.resume_pr("nonexistent-id-xyz")
    assert "unknown run_id" in str(exc.value)


def test_resume_picks_up_at_next_step(git_repo: Path, monkeypatch):
    spec = _spec(git_repo)
    init_run_dir(spec)

    # Pre-build state where commit is done; resume should run test+push+create.
    state = PRState(branch_name="luxe/bugfix/x")
    state.step("commit").done = True
    state.step("commit").status = "done"
    state.test_command = ""  # so test step skips
    save_pr_state(spec.run_id, state)

    # Mock _run so resume can complete without real git/gh
    calls: list[list[str]] = []

    def fake_run(cmd, cwd, env=None, timeout=None):
        calls.append(cmd)
        if cmd[:2] == ["git", "push"]:
            return CmdResult(rc=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return CmdResult(
                rc=0,
                stdout="https://github.com/user/repo/pull/77\n",
                stderr="",
            )
        return CmdResult(rc=0, stdout="", stderr="")

    monkeypatch.setattr(pr_mod, "_run", fake_run)

    state_after = pr_mod.resume_pr(spec.run_id)
    assert state_after.is_done("test")     # skipped → done
    assert state_after.is_done("push")
    assert state_after.is_done("create")
    assert state_after.pr_number == 77
    assert "https://github.com/user/repo/pull/77" in state_after.pr_url


def test_push_only_stops_after_push(git_repo: Path, monkeypatch):
    spec = _spec(git_repo)
    init_run_dir(spec)
    state = PRState(branch_name="luxe/bugfix/x")
    state.step("commit").done = True
    save_pr_state(spec.run_id, state)

    monkeypatch.setattr(pr_mod, "_run",
                        lambda cmd, cwd, env=None, timeout=None:
                        CmdResult(rc=0, stdout="", stderr=""))

    state_after = pr_mod.resume_pr(spec.run_id, push_only=True)
    assert state_after.is_done("push")
    # PR create should NOT have run
    assert not state_after.is_done("create")


def test_resume_preserves_failure_record(git_repo: Path, monkeypatch):
    spec = _spec(git_repo)
    init_run_dir(spec)
    state = PRState(branch_name="luxe/bugfix/x")
    state.step("commit").done = True
    save_pr_state(spec.run_id, state)

    # First push call fails; on disk, the step is marked failed.
    def fake_run(cmd, cwd, env=None, timeout=None):
        if cmd[:2] == ["git", "push"]:
            return CmdResult(rc=128, stdout="", stderr="auth failed")
        return CmdResult(rc=0, stdout="", stderr="")

    monkeypatch.setattr(pr_mod, "_run", fake_run)

    with pytest.raises(PRError):
        pr_mod.resume_pr(spec.run_id)

    saved = load_pr_state(spec.run_id)
    assert saved is not None
    push = saved.step_or_none("push")
    assert push is not None
    assert push.status == "failed"
    assert "auth failed" in push.detail
