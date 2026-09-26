"""scripts/regrade_local.py — a missing branch is UNREGRADABLE, never a
silent grade of base_sha ("the model changed nothing" counted as a FAIL)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.maintain_suite.grade import Fixture

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "regrade_local", ROOT / "scripts" / "regrade_local.py")
rl = importlib.util.module_from_spec(_spec)
sys.modules["regrade_local"] = rl
_spec.loader.exec_module(rl)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@t")
    _git(origin, "config", "user.name", "t")
    (origin / "a.py").write_text("x = 1\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "base")
    base = _git(origin, "rev-parse", "HEAD")
    _git(origin, "checkout", "-q", "-b", "luxe/implement/pushed")
    (origin / "a.py").write_text("x = 1\ny = 2  # MARK\n")
    _git(origin, "commit", "-q", "-am", "agent")
    _git(origin, "checkout", "-q", "main")
    fx = Fixture(id="fx", goal="g", task_type="implement",
                 expected_outcome={"kind": "regex_present", "pattern": "MARK"},
                 repo_url=str(origin), base_sha=base)
    return home, tmp_path, fx


def _seed(home: Path, out: Path, branch: str | None, *, diff: bool) -> Path:
    fdir = out / "acc" / "fx"
    fdir.mkdir(parents=True)
    (fdir / "result.json").write_text(json.dumps(
        {"fixture_id": "fx", "score": 4, "diff_produced": diff}))
    run_id = "r1"
    (fdir / "state.json").write_text(json.dumps({"luxe_run_id": run_id}))
    rd = home / ".luxe" / "runs" / run_id
    rd.mkdir(parents=True)
    if branch is not None:
        (rd / "pr_state.json").write_text(json.dumps({"branch_name": branch}))
    return fdir / "result.json"


def test_pushed_branch_is_graded(env):
    home, tmp, fx = env
    rp = _seed(home, tmp, "luxe/implement/pushed", diff=True)
    row = rl.regrade_one(rp, {"fx": fx})
    assert not row.get("unregradable")
    assert row["v2_outcome_passed"] is True


def test_missing_branch_is_unregradable_not_base(env):
    home, tmp, fx = env
    rp = _seed(home, tmp, "luxe/implement/never-pushed", diff=True)
    row = rl.regrade_one(rp, {"fx": fx})
    assert "unregradable" in row
    assert not rp.with_name("result_regraded.json").exists()


def test_no_branch_but_original_diff_is_unregradable(env):
    home, tmp, fx = env
    rp = _seed(home, tmp, None, diff=True)
    assert "unregradable" in rl.regrade_one(rp, {"fx": fx})


def test_no_branch_no_diff_grades_base(env):
    home, tmp, fx = env
    rp = _seed(home, tmp, None, diff=False)
    row = rl.regrade_one(rp, {"fx": fx})
    assert not row.get("unregradable")
    assert row["v2_passed"] is False


def test_worktree_is_a_private_tempdir(env, monkeypatch):
    home, tmp, fx = env
    rp = _seed(home, tmp, "luxe/implement/pushed", diff=True)
    seen = []
    real = rl._prepare_worktree

    def spy(fixture, branch, dest, **kw):
        seen.append(dest)
        return real(fixture, branch, dest, **kw)
    monkeypatch.setattr(rl, "_prepare_worktree", spy)
    rl.regrade_one(rp, {"fx": fx})
    assert seen and seen[0] != Path("/tmp") / "regrade-fx"
    assert not seen[0].exists()  # cleaned up
