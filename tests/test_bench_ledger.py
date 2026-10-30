"""Run-ledger audit (benchmarks/maintain_suite/ledger.py)."""

from __future__ import annotations

import json
from pathlib import Path

from benchmarks.maintain_suite import ledger
from benchmarks.maintain_suite import run as bench_run

BASE = "b" * 40


def _cell(root: Path, fid: str, *, vid: str = "", run_id: str = "", status: str = "done",
          head: str = "", base: str = BASE, diff: bool = True) -> Path:
    d = root / vid / fid if vid else root / fid
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({
        "fixture_id": fid, "status": status, "luxe_run_id": run_id or f"run-{vid}-{fid}",
        "base_sha_used": base, "head_sha_used": head}))
    if diff:
        (d / "diff.patch").write_text("diff --git a/x b/x\n")
    return d


def _kinds(report):
    return [p.kind for p in report.problems]


def test_clean_dir_is_ok(tmp_path):
    _cell(tmp_path, "f1", head="1" * 40)
    _cell(tmp_path, "f2", head="2" * 40)
    r = ledger.check_ledger(tmp_path, [("", "f1"), ("", "f2")])
    assert r.ok and r.states == 2 and r.legacy == 0


def test_duplicate_run_id_is_reported_first(tmp_path):
    _cell(tmp_path, "f1", run_id="dup", head="1" * 40, diff=False)
    _cell(tmp_path, "f2", run_id="dup", head="2" * 40)
    r = ledger.check_ledger(tmp_path)
    assert _kinds(r)[0] == "duplicate_run_id"
    assert set(_kinds(r)) == {"duplicate_run_id", "missing_diff"}


def test_shared_head_sha_is_reported(tmp_path):
    _cell(tmp_path, "f1", head="1" * 40)
    _cell(tmp_path, "f2", head="1" * 40)
    assert _kinds(ledger.check_ledger(tmp_path)) == ["shared_head_sha"]


def test_no_change_runs_may_share_head(tmp_path):
    """head == base means the run committed nothing; two such runs share base."""
    _cell(tmp_path, "f1", head=BASE)
    _cell(tmp_path, "f2", head=BASE)
    assert ledger.check_ledger(tmp_path).ok


def test_missing_diff_and_missing_cell(tmp_path):
    _cell(tmp_path, "f1", head="1" * 40, diff=False)
    r = ledger.check_ledger(tmp_path, [("", "f1"), ("", "f2")])
    assert sorted(_kinds(r)) == ["missing_cell", "missing_diff"]


def test_legacy_states_are_counted_not_failed(tmp_path):
    _cell(tmp_path, "f1", head="", diff=False)
    r = ledger.check_ledger(tmp_path)
    assert r.ok and r.legacy == 1


def test_non_done_states_skip_commit_rules(tmp_path):
    _cell(tmp_path, "f1", status="error", head="", diff=False)
    assert ledger.check_ledger(tmp_path).ok


def test_unreadable_state_is_reported(tmp_path):
    (tmp_path / "f1").mkdir()
    (tmp_path / "f1" / "state.json").write_text("{not json")
    assert _kinds(ledger.check_ledger(tmp_path)) == ["unreadable_state"]


def test_variant_layout_and_overlay_dir_skipped(tmp_path):
    _cell(tmp_path, "f1", vid="mono__a", head="1" * 40)
    _cell(tmp_path, "f1", vid="mono__b", head="2" * 40)
    _cell(tmp_path / "_overlays", "junk", run_id="run-mono__a-f1", head="1" * 40)
    r = ledger.check_ledger(tmp_path, [("mono__a", "f1"), ("mono__b", "f1")])
    assert r.ok and r.states == 2


def test_expected_scopes_the_audit_to_the_plan(tmp_path):
    """The default --output is ./acceptance: earlier benches live alongside.
    With a plan, only the plan's cells are audited."""
    _cell(tmp_path, "mine", head="1" * 40)
    _cell(tmp_path / "older_bench", "x", head="1" * 40)   # would collide if scanned
    assert ledger.check_ledger(tmp_path, [("", "mine")]).ok
    assert not ledger.check_ledger(tmp_path).ok           # unscoped sweep sees it


def test_render_and_cli(tmp_path, capsys):
    _cell(tmp_path, "f1", head="1" * 40)
    assert ledger.render(ledger.check_ledger(tmp_path)).endswith("OK")
    _cell(tmp_path, "f2", head="1" * 40)
    assert ledger.main([str(tmp_path)]) == 1
    assert "shared_head_sha" in capsys.readouterr().out


def test_report_ledger_never_raises(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("audit bug")
    monkeypatch.setattr(ledger, "check_ledger", boom)
    bench_run._report_ledger(tmp_path, [])
    assert "audit failed (RuntimeError: audit bug)" in capsys.readouterr().out
