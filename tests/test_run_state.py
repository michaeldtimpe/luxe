"""Tests for run_state RunSpec persistence."""

from __future__ import annotations

import pytest

from luxe.run_state import RunSpec, init_run_dir, load_run_spec


@pytest.fixture(autouse=True)
def _isolate_runs_root(tmp_path, monkeypatch):
    monkeypatch.setattr("luxe.run_state.runs_root", lambda: tmp_path / "runs")


def test_init_run_dir_writes_spec():
    spec = RunSpec(run_id="abc123def000", goal="fix bug", task_type="bugfix",
                   repo_path="/r", base_sha="deadbeef", base_branch="main")
    init_run_dir(spec)
    loaded = load_run_spec("abc123def000")
    assert loaded is not None
    assert loaded.goal == "fix bug"
    assert loaded.task_type == "bugfix"


def test_runspec_round_trip_through_disk():
    original = RunSpec(
        run_id="rt01", goal="test round trip",
        task_type="review", repo_path="/path/to/repo",
        base_sha="a" * 40, base_branch="main",
    )
    init_run_dir(original)
    loaded = load_run_spec("rt01")
    assert loaded.run_id == original.run_id
    assert loaded.goal == original.goal
    assert loaded.task_type == original.task_type
    assert loaded.base_sha == original.base_sha
    assert loaded.started_at == original.started_at


def test_unknown_run_id_returns_none():
    assert load_run_spec("does-not-exist") is None
