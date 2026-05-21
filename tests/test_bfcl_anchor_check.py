"""Tests for scripts/bfcl_anchor_check.py — flip detection + hard gates."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.bfcl_anchor_check import check_gates, compare, load_results, main


def _make_run(root: Path, problems: list[tuple[str, str, bool, str]]) -> Path:
    """problems = [(id, category, passed, reason), ...]"""
    for pid, cat, passed, reason in problems:
        (root / cat).mkdir(parents=True, exist_ok=True)
        (root / cat / f"{pid}.json").write_text(json.dumps({
            "id": pid, "category": cat, "passed": passed, "reason": reason,
            "wall_s": 10.0, "prompt_tokens": 100, "completion_tokens": 100,
            "actual_calls": [], "error": "",
        }))
    return root


def test_load_results_reads_all_categories(tmp_path: Path):
    root = _make_run(tmp_path / "run", [
        ("simple_python_0", "simple_python", True, "matched_gt_entry"),
        ("irrelevance_0", "irrelevance", True, "no_tool_call_correctly_emitted"),
        ("parallel_0", "parallel", False, "emitted_1_calls_expected_2"),
    ])
    out = load_results(root)
    assert set(out.keys()) == {"simple_python_0", "irrelevance_0", "parallel_0"}
    assert out["simple_python_0"]["category"] == "simple_python"


def test_load_results_filters_to_requested_categories(tmp_path: Path):
    root = _make_run(tmp_path / "run", [
        ("simple_python_0", "simple_python", True, ""),
        ("simple_python_1", "simple_python", True, ""),
        ("irrelevance_0", "irrelevance", True, ""),
        ("parallel_0", "parallel", False, ""),
    ])
    out = load_results(root, categories=("irrelevance",))
    assert set(out.keys()) == {"irrelevance_0"}
    out = load_results(root, categories=("simple_python", "parallel"))
    assert set(out.keys()) == {"simple_python_0", "simple_python_1", "parallel_0"}


def test_main_categories_subset_skips_total_gate(tmp_path: Path):
    """Phase 3b cheap-probe pattern: irrelevance-only run, TOTAL gate auto-skipped
    because TOTAL on a subset is not comparable to the full-anchor 90.24% floor."""
    a = _make_run(tmp_path / "a", [
        ("ir1", "irrelevance", True, ""),
        ("sp1", "simple_python", True, ""),
        ("sp2", "simple_python", False, ""),
    ])
    b = _make_run(tmp_path / "b", [
        ("ir1", "irrelevance", True, ""),
        # Note: even with simple_python both failing in `b`, the TOTAL gate
        # is skipped because we restrict to irrelevance only.
        ("sp1", "simple_python", False, ""),
        ("sp2", "simple_python", False, ""),
    ])
    rc = main(["--anchor", str(a), "--new", str(b),
               "--categories", "irrelevance",
               "--total-floor", "99.0",  # would fail if not skipped
               "--irrelevance-floor", "1"])
    assert rc == 0


def test_main_categories_subset_still_enforces_irrelevance_floor(tmp_path: Path):
    a = _make_run(tmp_path / "a", [
        ("ir1", "irrelevance", True, ""),
        ("ir2", "irrelevance", True, ""),
        ("sp1", "simple_python", True, ""),
    ])
    b = _make_run(tmp_path / "b", [
        ("ir1", "irrelevance", True, ""),
        ("ir2", "irrelevance", False, "emitted_tool"),  # regression
        ("sp1", "simple_python", True, ""),
    ])
    rc = main(["--anchor", str(a), "--new", str(b),
               "--categories", "irrelevance",
               "--total-floor", "0.0",
               "--irrelevance-floor", "2"])
    assert rc == 1


def test_compare_perfect_agreement_no_flips(tmp_path: Path):
    a = _make_run(tmp_path / "a", [
        ("p1", "simple_python", True, "x"),
        ("p2", "irrelevance", True, "y"),
    ])
    b = _make_run(tmp_path / "b", [
        ("p1", "simple_python", True, "x"),
        ("p2", "irrelevance", True, "y"),
    ])
    r = compare(load_results(a), load_results(b))
    assert r["total_P_to_F"] == 0
    assert r["total_F_to_P"] == 0
    assert r["agreement_pct"] == 100.0


def test_compare_detects_P_to_F_flip(tmp_path: Path):
    a = _make_run(tmp_path / "a", [
        ("p1", "simple_python", True, "x"),
    ])
    b = _make_run(tmp_path / "b", [
        ("p1", "simple_python", False, "regressed"),
    ])
    r = compare(load_results(a), load_results(b))
    assert r["total_P_to_F"] == 1
    assert r["per_category"]["simple_python"]["P_to_F"] == ["p1"]


def test_compare_detects_F_to_P_flip(tmp_path: Path):
    a = _make_run(tmp_path / "a", [
        ("p1", "simple_python", False, "x"),
    ])
    b = _make_run(tmp_path / "b", [
        ("p1", "simple_python", True, "recovered"),
    ])
    r = compare(load_results(a), load_results(b))
    assert r["total_F_to_P"] == 1


def test_compare_per_category_aggregation(tmp_path: Path):
    a = _make_run(tmp_path / "a", [
        ("ir1", "irrelevance", True, ""),
        ("ir2", "irrelevance", True, ""),
        ("sp1", "simple_python", True, ""),
        ("sp2", "simple_python", False, ""),
    ])
    b = _make_run(tmp_path / "b", [
        ("ir1", "irrelevance", True, ""),
        ("ir2", "irrelevance", True, ""),
        ("sp1", "simple_python", True, ""),
        ("sp2", "simple_python", True, ""),
    ])
    r = compare(load_results(a), load_results(b))
    sp = r["per_category"]["simple_python"]
    ir = r["per_category"]["irrelevance"]
    assert sp["anchor_pass"] == 1 and sp["new_pass"] == 2
    assert ir["anchor_pass"] == 2 and ir["new_pass"] == 2
    assert r["total_anchor_pass"] == 3 and r["total_new_pass"] == 4


def test_check_gates_total_floor_pass():
    r = {"total_new_pct": 90.24,
         "per_category": {"irrelevance": {"new_pass": 240}}}
    g = check_gates(r, total_floor=90.00, irrelevance_floor=240)
    assert g["total_pct_gate"] is True
    assert g["irrelevance_gate"] is True


def test_check_gates_total_floor_fail():
    r = {"total_new_pct": 89.99,
         "per_category": {"irrelevance": {"new_pass": 240}}}
    g = check_gates(r, total_floor=90.00, irrelevance_floor=240)
    assert g["total_pct_gate"] is False
    assert g["irrelevance_gate"] is True


def test_check_gates_irrelevance_fail():
    r = {"total_new_pct": 95.00,
         "per_category": {"irrelevance": {"new_pass": 239}}}
    g = check_gates(r, total_floor=90.00, irrelevance_floor=240)
    assert g["total_pct_gate"] is True
    assert g["irrelevance_gate"] is False


def test_main_returns_0_when_both_gates_clear(tmp_path: Path):
    a = _make_run(tmp_path / "a", [
        ("ir1", "irrelevance", True, ""),
        ("sp1", "simple_python", True, ""),
    ])
    b = _make_run(tmp_path / "b", [
        ("ir1", "irrelevance", True, ""),
        ("sp1", "simple_python", True, ""),
    ])
    rc = main(["--anchor", str(a), "--new", str(b),
               "--total-floor", "50", "--irrelevance-floor", "1"])
    assert rc == 0


def test_main_returns_1_when_total_floor_misses(tmp_path: Path):
    a = _make_run(tmp_path / "a", [
        ("p1", "simple_python", True, ""),
        ("p2", "simple_python", True, ""),
    ])
    b = _make_run(tmp_path / "b", [
        ("p1", "simple_python", False, ""),
        ("p2", "simple_python", False, ""),
    ])
    rc = main(["--anchor", str(a), "--new", str(b),
               "--total-floor", "50.0", "--irrelevance-floor", "0"])
    assert rc == 1


def test_main_returns_1_when_irrelevance_floor_misses(tmp_path: Path):
    a = _make_run(tmp_path / "a", [
        ("ir1", "irrelevance", True, ""),
    ])
    b = _make_run(tmp_path / "b", [
        ("ir1", "irrelevance", False, "emitted_tool_call"),
    ])
    rc = main(["--anchor", str(a), "--new", str(b),
               "--total-floor", "0.0", "--irrelevance-floor", "1"])
    assert rc == 1


def test_main_returns_2_on_missing_anchor(tmp_path: Path):
    b = _make_run(tmp_path / "b", [("p1", "simple_python", True, "")])
    rc = main(["--anchor", str(tmp_path / "nonexistent"), "--new", str(b)])
    assert rc == 2


def test_main_snapshot_jsonl_emits_one_row_per_problem(tmp_path: Path):
    a = _make_run(tmp_path / "a", [
        ("p1", "simple_python", True, "matched_gt_entry"),
        ("p2", "irrelevance", False, "emitted_tool"),
    ])
    b = _make_run(tmp_path / "b", [
        ("p1", "simple_python", False, "regressed"),
        ("p2", "irrelevance", True, "correctly_declined"),
    ])
    snap = tmp_path / "snap.jsonl"
    main(["--anchor", str(a), "--new", str(b),
          "--total-floor", "0.0", "--irrelevance-floor", "0",
          "--snapshot-out", str(snap)])
    lines = snap.read_text().splitlines()
    assert len(lines) == 2
    rows = {json.loads(l)["id"]: json.loads(l) for l in lines}
    assert rows["p1"]["anchor_pass"] is True and rows["p1"]["new_pass"] is False
    assert rows["p1"]["new_reason"] == "regressed"
    assert rows["p2"]["anchor_reason"] == "emitted_tool"
