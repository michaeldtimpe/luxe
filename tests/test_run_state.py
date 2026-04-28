"""Tests for run_state stage checkpointing primitives."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from luxe.run_state import (
    RunSpec,
    clear_stages,
    init_run_dir,
    list_completed_stages,
    load_run_spec,
    load_stage,
    save_stage,
    stages_dir,
)


@pytest.fixture(autouse=True)
def _isolate_runs_root(tmp_path, monkeypatch):
    monkeypatch.setattr("luxe.run_state.runs_root", lambda: tmp_path / "runs")


def test_init_run_dir_writes_spec():
    spec = RunSpec(run_id="abc123def000", goal="fix bug", task_type="bugfix",
                   repo_path="/r", base_sha="deadbeef", base_branch="main",
                   actual_mode="swarm")
    init_run_dir(spec)
    loaded = load_run_spec("abc123def000")
    assert loaded is not None
    assert loaded.goal == "fix bug"
    assert loaded.actual_mode == "swarm"


def test_save_and_load_stage():
    spec = RunSpec(run_id="run01")
    init_run_dir(spec)
    save_stage("run01", "architect", {"objectives": [{"title": "x"}], "wall_s": 1.5})
    loaded = load_stage("run01", "architect")
    assert loaded is not None
    assert loaded["objectives"] == [{"title": "x"}]
    assert loaded["wall_s"] == 1.5


def test_load_stage_returns_none_for_missing():
    spec = RunSpec(run_id="run02")
    init_run_dir(spec)
    assert load_stage("run02", "architect") is None


def test_save_stage_atomic():
    """save_stage should not leave a partial file on disk if write fails."""
    spec = RunSpec(run_id="run03")
    init_run_dir(spec)
    save_stage("run03", "synth", {"final_report": "hello"})
    # Stale .tmp file should not exist after the rename
    sd = stages_dir("run03")
    assert (sd / "synth.json").is_file()
    assert not list(sd.glob("*.tmp"))


def test_list_completed_stages_sorted():
    spec = RunSpec(run_id="run04")
    init_run_dir(spec)
    save_stage("run04", "validator", {})
    save_stage("run04", "architect", {})
    save_stage("run04", "worker_0", {})
    stages = list_completed_stages("run04")
    assert stages == ["architect", "validator", "worker_0"]


def test_clear_stages_removes_all():
    spec = RunSpec(run_id="run05")
    init_run_dir(spec)
    save_stage("run05", "architect", {})
    save_stage("run05", "validator", {})
    n = clear_stages("run05")
    assert n == 2
    assert list_completed_stages("run05") == []


def test_clear_stages_no_op_when_no_stages():
    spec = RunSpec(run_id="run06")
    init_run_dir(spec)
    assert clear_stages("run06") == 0


def test_runspec_round_trip_through_disk():
    original = RunSpec(
        run_id="rt01", goal="test round trip",
        mode="auto", actual_mode="single",
        task_type="review", repo_path="/path/to/repo",
        base_sha="a" * 40, base_branch="main",
    )
    init_run_dir(original)
    loaded = load_run_spec("rt01")
    assert loaded.run_id == original.run_id
    assert loaded.goal == original.goal
    assert loaded.actual_mode == original.actual_mode
    assert loaded.base_sha == original.base_sha
    assert loaded.started_at == original.started_at


def test_unknown_run_id_returns_none():
    assert load_run_spec("does-not-exist") is None
