"""Tests for the acceptance-suite runner's resume / state logic.

The runner has three layers: per-fixture status, per-stage checkpoint
inspection (delegating to luxe), and PR-step resume (delegated to luxe pr).
We test:
  - state save/load round-trip
  - decide() picks SKIP_DONE / SKIP_REQUIRED_ENV / RUN_FRESH / RUN_RESUME
  - run_fixture honours --force, --retry-errors, --dry-run
  - artefact reader pulls validator/citations/tokens correctly
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from benchmarks.maintain_suite.grade import Fixture, FixtureResult
import benchmarks.maintain_suite.run as br
from benchmarks.maintain_suite.run import (
    Decision,
    Diagnostics,
    FixtureState,
    FixtureStatus,
    _luxe_run_dir,
    _stderr_excerpt,
    decide,
    load_state,
    run_fixture,
    save_state,
)


@pytest.fixture(autouse=True)
def _isolate_luxe_runs(tmp_path, monkeypatch):
    fake_runs = tmp_path / "fake-luxe-runs"
    fake_runs.mkdir(parents=True)
    monkeypatch.setattr(br, "_luxe_run_dir", lambda rid: fake_runs / rid)


def _f(id_="f1", *, task_type="bugfix", required_env=()) -> Fixture:
    return Fixture(
        id=id_, goal="g", task_type=task_type,
        expected_outcome={"kind": "regex_present", "pattern": "anything"},
        repo_url="", repo_path="/tmp/nope",
        required_env=list(required_env),
    )


def _seed_run_dir(tmp_path, run_id, *, stages=(),
                  pr_steps_done=False) -> Path:
    rd = tmp_path / "fake-luxe-runs" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(json.dumps({"run_id": run_id}))
    if stages:
        sd = rd / "stages"
        sd.mkdir(parents=True, exist_ok=True)
        for s in stages:
            (sd / f"{s}.json").write_text("{}")
    if pr_steps_done:
        (rd / "pr_state.json").write_text(json.dumps({
            "branch_name": "luxe/x", "pr_number": 1, "pr_url": "u",
            "test_command": "", "test_passed": True, "is_draft": False,
            "test_output_tail": "",
            "steps": [
                {"name": "commit", "done": True, "status": "done", "detail": "", "completed_at": 0.0},
                {"name": "test", "done": True, "status": "done", "detail": "", "completed_at": 0.0},
                {"name": "push", "done": True, "status": "done", "detail": "", "completed_at": 0.0},
                {"name": "create", "done": True, "status": "done", "detail": "", "completed_at": 0.0},
            ],
        }))
    return rd


# --- state round-trip --

def test_state_round_trip(tmp_path: Path):
    out = tmp_path / "acc"
    s = FixtureState(fixture_id="abc", status=FixtureStatus.RUNNING,
                     luxe_run_id="r123", attempts=2, last_error="boom")
    save_state(out, s)
    loaded = load_state(out, "abc")
    assert loaded.status == FixtureStatus.RUNNING
    assert loaded.luxe_run_id == "r123"
    assert loaded.attempts == 2
    assert loaded.last_error == "boom"


def test_state_default_when_missing(tmp_path: Path):
    out = tmp_path / "acc"
    out.mkdir()
    s = load_state(out, "fresh")
    assert s.status == FixtureStatus.PENDING
    assert s.luxe_run_id == ""


# --- decide() --

def test_decide_skip_done():
    f = _f()
    s = FixtureState(fixture_id="f1", status=FixtureStatus.DONE)
    d, reason = decide(f, s, force=False, retry_errors=False, retry_skipped=False)
    assert d == Decision.SKIP_DONE
    assert "already done" in reason


def test_decide_force_overrides_done():
    f = _f()
    s = FixtureState(fixture_id="f1", status=FixtureStatus.DONE,
                     luxe_run_id="cached")
    d, _ = decide(f, s, force=True, retry_errors=False, retry_skipped=False)
    assert d == Decision.RUN_FRESH


def test_decide_skip_required_env_missing(monkeypatch):
    monkeypatch.delenv("MY_SECRET", raising=False)
    f = _f(required_env=["MY_SECRET"])
    s = FixtureState(fixture_id="f1", status=FixtureStatus.PENDING)
    d, reason = decide(f, s, force=False, retry_errors=False, retry_skipped=False)
    assert d == Decision.SKIP_REQUIRED_ENV
    assert "MY_SECRET" in reason


def test_decide_skip_error_unless_retry():
    f = _f()
    s = FixtureState(fixture_id="f1", status=FixtureStatus.ERROR,
                     last_error="boom")
    d_no, _ = decide(f, s, force=False, retry_errors=False, retry_skipped=False)
    assert d_no == Decision.SKIP_DONE
    d_yes, _ = decide(f, s, force=False, retry_errors=True, retry_skipped=False)
    assert d_yes == Decision.RUN_FRESH


def test_decide_skip_skipped_unless_retry():
    f = _f()
    s = FixtureState(fixture_id="f1", status=FixtureStatus.SKIPPED)
    d_no, _ = decide(f, s, force=False, retry_errors=False, retry_skipped=False)
    assert d_no == Decision.SKIP_DONE
    d_yes, _ = decide(f, s, force=False, retry_errors=False, retry_skipped=True)
    assert d_yes == Decision.RUN_FRESH


def test_decide_run_fresh_for_pending():
    f = _f()
    s = FixtureState(fixture_id="f1", status=FixtureStatus.PENDING)
    d, reason = decide(f, s, force=False, retry_errors=False, retry_skipped=False)
    assert d == Decision.RUN_FRESH
    assert "new run" in reason


def test_decide_run_resume_when_run_dir_has_stages(tmp_path):
    _seed_run_dir(tmp_path, "r99", stages=("architect",))
    f = _f()
    s = FixtureState(fixture_id="f1", status=FixtureStatus.RUNNING,
                     luxe_run_id="r99")
    d, reason = decide(f, s, force=False, retry_errors=False, retry_skipped=False)
    assert d == Decision.RUN_RESUME
    assert "architect" in reason


def test_decide_run_resume_when_pipeline_complete_but_pr_incomplete(tmp_path):
    _seed_run_dir(tmp_path, "r88",
                  stages=("architect", "worker_0", "validator", "synthesizer"))
    f = _f()
    s = FixtureState(fixture_id="f1", status=FixtureStatus.RUNNING,
                     luxe_run_id="r88")
    d, _ = decide(f, s, force=False, retry_errors=False, retry_skipped=False)
    assert d == Decision.RUN_RESUME


def test_decide_force_clears_cached_run(tmp_path):
    _seed_run_dir(tmp_path, "rOLD", stages=("architect",))
    f = _f()
    s = FixtureState(fixture_id="f1", status=FixtureStatus.DONE,
                     luxe_run_id="rOLD")
    d, _ = decide(f, s, force=True, retry_errors=False, retry_skipped=False)
    assert d == Decision.RUN_FRESH


# --- run_fixture (high level) --

def test_run_fixture_skip_required_env_persists_state(tmp_path, monkeypatch):
    monkeypatch.delenv("REQ_ENV_X", raising=False)
    out = tmp_path / "acc"
    f = _f(required_env=["REQ_ENV_X"])
    fr, diag = run_fixture(f, out, tmp_path / "wd")
    assert fr.skipped
    assert "REQ_ENV_X" in fr.skipped_reason
    state = load_state(out, "f1")
    assert state.status == FixtureStatus.SKIPPED


def test_run_fixture_dry_run_no_subprocess(tmp_path, monkeypatch):
    """--dry-run should not invoke luxe and should not write result.json."""
    out = tmp_path / "acc"
    f = _f()
    monkeypatch.setattr(br, "_resolve_repo",
                        lambda fix, wd: (Path("/tmp"), ""))
    # If dry_run actually invoked luxe, the test would hang/fail; this proves
    # the code path is short-circuited.
    fr, diag = run_fixture(f, out, tmp_path / "wd", dry_run=True)
    assert fr.skipped
    assert fr.skipped_reason == "dry_run"
    # No result.json written for dry-run.
    assert not (out / "f1" / "result.json").is_file()


def test_run_fixture_already_done_returns_cached(tmp_path):
    out = tmp_path / "acc"
    fdir = out / "f1"
    fdir.mkdir(parents=True)
    save_state(out, FixtureState(fixture_id="f1", status=FixtureStatus.DONE,
                                  luxe_run_id="rXY"))
    cached = FixtureResult(fixture_id="f1", score=5, pr_opened=True,
                            pr_url="u", expected_outcome_passed=True)
    (fdir / "result.json").write_text(json.dumps(cached.to_dict()))
    fr, diag = run_fixture(_f(), out, tmp_path / "wd")
    # Returned result reflects the cached score (5, passed)
    assert fr.score == 5
    assert fr.pr_opened


def test_run_fixture_resolve_repo_failure_marks_error(tmp_path, monkeypatch):
    out = tmp_path / "acc"
    monkeypatch.setattr(br, "_resolve_repo",
                        lambda fix, wd: (None, "no repo here"))
    fr, diag = run_fixture(_f(), out, tmp_path / "wd")
    assert fr.error
    assert "no repo here" in fr.error
    state = load_state(out, "f1")
    assert state.status == FixtureStatus.ERROR


# --- artefact reader --

def test_read_run_artefacts_validator_done(tmp_path):
    rid = "rA"
    rd = _seed_run_dir(tmp_path, rid)
    events = rd / "events.jsonl"
    events.write_text("\n".join([
        json.dumps({"kind": "validator_done", "status": "verified",
                    "verified_count": 3, "removed_count": 1}),
        json.dumps({"kind": "citation_lint_passed", "count": 5}),
        json.dumps({"kind": "finish", "total_wall_s": 42.5}),
    ]))
    artefacts = br._read_run_artefacts(rid)
    assert artefacts["validator_status"] == "verified"
    assert artefacts["validator_verified"] == 3
    assert artefacts["validator_removed"] == 1
    assert artefacts["citations_total"] == 5
    assert artefacts["citations_unresolved"] == 0
    assert artefacts["wall_s_total"] == 42.5


def test_read_run_artefacts_citation_blocked(tmp_path):
    rid = "rB"
    rd = _seed_run_dir(tmp_path, rid)
    (rd / "events.jsonl").write_text(json.dumps({
        "kind": "citation_lint_blocked", "unresolved": 3, "summary": "..."
    }))
    artefacts = br._read_run_artefacts(rid)
    assert artefacts["citations_unresolved"] == 3


def test_read_run_artefacts_pr_state(tmp_path):
    rid = "rC"
    rd = _seed_run_dir(tmp_path, rid)
    (rd / "pr_state.json").write_text(json.dumps({
        "pr_url": "https://gh/...", "is_draft": False,
        "test_passed": True, "steps": [],
    }))
    artefacts = br._read_run_artefacts(rid)
    assert artefacts["pr_opened"] is True
    assert artefacts["pr_url"] == "https://gh/..."
    assert artefacts["test_passed"] is True


def test_read_run_artefacts_resumed_stages(tmp_path):
    rid = "rD"
    rd = _seed_run_dir(tmp_path, rid)
    (rd / "events.jsonl").write_text("\n".join([
        json.dumps({"kind": "architect_resumed", "objectives": 4}),
        json.dumps({"kind": "worker_resumed", "index": 0}),
        json.dumps({"kind": "validator_resumed", "status": "verified",
                    "verified_count": 1}),
    ]))
    a = br._read_run_artefacts(rid)
    assert "architect" in a["stages_resumed"]
    assert "worker_0" in a["stages_resumed"]
    assert "validator" in a["stages_resumed"]


# --- pipeline-completion gates --

def test_luxe_pipeline_complete_only_when_synthesizer_present(tmp_path):
    _seed_run_dir(tmp_path, "rE", stages=("architect", "worker_0"))
    assert not br._luxe_pipeline_complete("rE")
    _seed_run_dir(tmp_path, "rF", stages=("architect", "worker_0",
                                            "validator", "synthesizer"))
    assert br._luxe_pipeline_complete("rF")


def test_luxe_pr_complete_when_no_pr_state(tmp_path):
    _seed_run_dir(tmp_path, "rG", stages=("synthesizer",))
    # No pr_state.json → considered complete (read-only task)
    assert br._luxe_pr_complete("rG")


def test_luxe_pr_complete_all_steps_done(tmp_path):
    _seed_run_dir(tmp_path, "rH", stages=("synthesizer",), pr_steps_done=True)
    assert br._luxe_pr_complete("rH")


# --- aggregate diagnostics tuning hints --

def test_tuning_hints_validator_ambiguous():
    diags = [Diagnostics(fixture_id=f"f{i}", validator_status="ambiguous") for i in range(4)]
    diags += [Diagnostics(fixture_id="g", validator_status="verified")]
    hints = br._tuning_hints(diags, [])
    assert any("ambiguous" in h for h in hints)


def test_tuning_hints_citation_blocked():
    diags = [Diagnostics(fixture_id=f"f{i}", citations_unresolved=2) for i in range(3)]
    diags += [Diagnostics(fixture_id=f"g{i}") for i in range(7)]
    hints = br._tuning_hints(diags, [])
    assert any("citation_lint_blocked" in h for h in hints)


def test_tuning_hints_long_runs():
    diags = [Diagnostics(fixture_id="x", wall_s=2400)]  # 40 min
    hints = br._tuning_hints(diags, [])
    assert any("30 min" in h for h in hints)


def test_tuning_hints_test_failures():
    diags = [Diagnostics(fixture_id="x", test_passed=False)]
    hints = br._tuning_hints(diags, [])
    assert any("failing tests" in h for h in hints)


# --- stderr excerpt --

def test_stderr_excerpt_short_passthrough():
    assert _stderr_excerpt("boom") == "boom"


def test_stderr_excerpt_empty():
    assert _stderr_excerpt("") == ""
    assert _stderr_excerpt(None) == ""


def test_stderr_excerpt_long_truncates_to_tail():
    long = "header\n" + "x" * 1000 + "\nlast meaningful line"
    out = _stderr_excerpt(long, max_chars=100)
    assert out.startswith("...(truncated)")
    assert "last meaningful line" in out
    assert len(out) <= len("...(truncated) ") + 100


def test_heal_stale_silent_failure_done_to_error(tmp_path):
    """A DONE state with cached diagnostics showing wall<5s + tokens=0 must
    be reclassified to ERROR so --retry-errors picks it up. Pre-fix builds
    produced this state for every silent-failed fixture."""
    out = tmp_path / "acc"
    fid = "stale-silent"
    save_state(out, FixtureState(fixture_id=fid, status=FixtureStatus.DONE,
                                  luxe_run_id="r1"))
    diag_path = out / fid / "diagnostics.json"
    diag_path.write_text(json.dumps({
        "fixture_id": fid, "run_id": "r1", "wall_s": 0.0, "tokens_total": 0,
        "stages_completed": [], "stages_resumed": [],
        "validator_status": "", "validator_verified": 0, "validator_removed": 0,
        "citations_unresolved": 0, "citations_total": 0,
        "pr_url": "", "pr_opened": False, "is_draft": False,
        "test_passed": None, "events_kinds": {},
    }))
    state = load_state(out, fid)
    assert state.status == FixtureStatus.DONE
    healed = br._heal_stale_silent_failure(state, out)
    assert healed
    assert state.status == FixtureStatus.ERROR
    assert "silent failure" in state.last_error
    # Persisted to disk too
    reloaded = load_state(out, fid)
    assert reloaded.status == FixtureStatus.ERROR


def test_heal_does_not_reclassify_real_done(tmp_path):
    """A DONE state with cached diagnostics showing real work (tokens > 0)
    must NOT be reclassified — it was a legitimate completed run."""
    out = tmp_path / "acc"
    fid = "real-done"
    save_state(out, FixtureState(fixture_id=fid, status=FixtureStatus.DONE,
                                  luxe_run_id="r1"))
    diag_path = out / fid / "diagnostics.json"
    diag_path.write_text(json.dumps({
        "fixture_id": fid, "run_id": "r1", "wall_s": 87.5, "tokens_total": 42810,
        "stages_completed": ["architect", "worker_0"], "stages_resumed": [],
        "validator_status": "verified", "validator_verified": 3,
        "validator_removed": 0,
        "citations_unresolved": 0, "citations_total": 3,
        "pr_url": "https://...", "pr_opened": True, "is_draft": False,
        "test_passed": True, "events_kinds": {},
    }))
    state = load_state(out, fid)
    healed = br._heal_stale_silent_failure(state, out)
    assert not healed
    assert state.status == FixtureStatus.DONE


def test_heal_no_op_without_cached_diag(tmp_path):
    out = tmp_path / "acc"
    fid = "no-diag"
    save_state(out, FixtureState(fixture_id=fid, status=FixtureStatus.DONE))
    state = load_state(out, fid)
    assert not br._heal_stale_silent_failure(state, out)
    assert state.status == FixtureStatus.DONE


def test_run_fixture_silent_failure_marks_state_error(tmp_path, monkeypatch):
    """Going forward, when run_fixture grades a silent-failed run, it must
    save state as ERROR so subsequent --retry-errors picks it up."""
    out = tmp_path / "acc"
    monkeypatch.setattr(br, "_resolve_repo",
                        lambda fix, wd: (Path("/tmp"), ""))
    monkeypatch.setattr(br, "_head_sha", lambda repo: "abc" * 13 + "d")
    monkeypatch.setattr(br, "_luxe_maintain",
                        lambda repo, fix, log_dir: (0, "abcdef123456", ""))
    # Read artefacts returns a silent-failure shape
    monkeypatch.setattr(br, "_read_run_artefacts",
                        lambda rid: {
                            "pr_url": "", "pr_opened": False, "is_draft": False,
                            "test_passed": None,
                            "citations_unresolved": 0, "citations_total": 0,
                            "validator_status": "", "validator_verified": 0,
                            "validator_removed": 0,
                            "stages_completed": [], "stages_resumed": [],
                            "tokens_total": 0, "wall_s_total": 0.0,
                            "events_kinds": {},
                        })
    # grade_fixture is real — uses the real repo (any directory works since
    # diff is empty). Avoid the gh / git calls by patching _changed_files.
    from benchmarks.maintain_suite import grade as grade_mod
    monkeypatch.setattr(grade_mod, "_changed_files", lambda repo, sha: [])

    fr, diag = run_fixture(_f(), out, tmp_path / "wd")
    state = load_state(out, "f1")
    assert state.status == FixtureStatus.ERROR
    assert "silent failure" in state.last_error
    # Result + diag artefacts ARE persisted (useful breadcrumbs)
    assert (out / "f1" / "result.json").is_file()
    assert (out / "f1" / "diagnostics.json").is_file()


def test_run_fixture_surfaces_stderr_excerpt(tmp_path, monkeypatch):
    """When luxe.cli fails to start, the stderr should be captured into
    state.last_error so the user sees what broke without grepping logs."""
    out = tmp_path / "acc"
    monkeypatch.setattr(br, "_resolve_repo",
                        lambda fix, wd: (Path("/tmp"), ""))
    monkeypatch.setattr(br, "_head_sha", lambda repo: "deadbeef" * 5)

    def fake_maintain(repo, fixture, log_dir):
        return 1, "", "ModuleNotFoundError: No module named 'luxe'"
    monkeypatch.setattr(br, "_luxe_maintain", fake_maintain)

    fr, diag = run_fixture(_f(), out, tmp_path / "wd")
    assert fr.error
    assert "ModuleNotFoundError" in fr.error
    state = load_state(out, "f1")
    assert state.status == FixtureStatus.ERROR
    assert "ModuleNotFoundError" in state.last_error
