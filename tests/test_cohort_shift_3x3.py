"""Tests for scripts/cohort_shift_3x3.py — per-instance classification + exit gate.

The classifier is pure (no I/O, no env) — fixtures inline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.cohort_shift_3x3 import (
    build_instance_tiers,
    classify_instance,
    cohort_shift,
    main,
    median_rank,
    tier_rank,
)


def test_tier_rank_known_tiers_ordered():
    assert tier_rank("strong") < tier_rank("plausible") < tier_rank("wrong_location")
    assert tier_rank("wrong_location") < tier_rank("wrong_target")
    assert tier_rank("wrong_target") < tier_rank("new_file_in_diff") < tier_rank("empty_patch")


def test_tier_rank_unknown_falls_back_to_99():
    assert tier_rank("MISSING") == 99
    assert tier_rank("nonsense") == 99


def test_median_rank_uniform_returns_that_rank():
    assert median_rank(("strong", "strong", "strong")) == 1.0


def test_median_rank_odd_count_picks_middle():
    assert median_rank(("strong", "plausible", "empty_patch")) == 2.0


def test_median_rank_even_count_averages():
    assert median_rank(("strong", "plausible")) == 1.5


def test_classify_byte_identical_when_all_six_tiers_match():
    v = classify_instance(("strong",) * 3, ("strong",) * 3)
    assert v["verdict"] == "byte_identical"
    assert v["rank_delta"] == 0


def test_classify_deterministic_gain_empty_to_strong():
    v = classify_instance(("empty_patch",) * 3, ("strong",) * 3)
    assert v["verdict"] == "deterministic_gain"
    assert v["rank_delta"] < 0
    assert v["a_uniform_tier"] == "empty_patch"
    assert v["b_uniform_tier"] == "strong"


def test_classify_deterministic_loss_strong_to_empty():
    v = classify_instance(("strong",) * 3, ("empty_patch",) * 3)
    assert v["verdict"] == "deterministic_loss"
    assert v["rank_delta"] > 0


def test_classify_modal_gain_when_median_improves_with_variance():
    # A: 2 plausible + 1 empty (median plausible=2). B: 3 strong (median strong=1).
    v = classify_instance(
        ("plausible", "plausible", "empty_patch"),
        ("strong", "strong", "strong"),
    )
    # B-uniform-strong while A is non-uniform → falls through to modal_gain.
    assert v["verdict"] == "modal_gain"
    assert v["rank_delta"] < 0


def test_classify_modal_loss_when_median_regresses():
    v = classify_instance(
        ("strong", "strong", "plausible"),
        ("plausible", "wrong_location", "wrong_location"),
    )
    assert v["verdict"] == "modal_loss"


def test_classify_noise_when_medians_match_with_variance():
    # Both have median plausible (rank 2) but with different variance.
    v = classify_instance(
        ("strong", "plausible", "wrong_location"),
        ("plausible", "plausible", "plausible"),
    )
    assert v["verdict"] == "noise"
    assert v["rank_delta"] == 0


def test_classify_uniform_same_tier_is_byte_identical():
    # Edge case: both cycles uniform on the same tier.
    v = classify_instance(("strong",) * 3, ("strong",) * 3)
    assert v["verdict"] == "byte_identical"


def test_build_instance_tiers_collects_across_reps():
    rep1 = {"i1": "strong", "i2": "empty_patch"}
    rep2 = {"i1": "strong", "i2": "plausible"}
    rep3 = {"i1": "plausible", "i2": "empty_patch"}
    out = build_instance_tiers([rep1, rep2, rep3])
    assert out["i1"] == ("strong", "strong", "plausible")
    assert out["i2"] == ("empty_patch", "plausible", "empty_patch")


def test_build_instance_tiers_marks_missing_instance():
    rep1 = {"i1": "strong"}
    rep2 = {"i1": "strong", "i2": "empty_patch"}
    out = build_instance_tiers([rep1, rep2])
    assert out["i2"] == ("MISSING", "empty_patch")


def test_cohort_shift_groups_per_instance_verdicts():
    cycle_a = {"i_loss": ("strong",) * 3, "i_gain": ("empty_patch",) * 3}
    cycle_b = {"i_loss": ("empty_patch",) * 3, "i_gain": ("strong",) * 3}
    out = cohort_shift(cycle_a, cycle_b)
    assert out["i_loss"]["verdict"] == "deterministic_loss"
    assert out["i_gain"]["verdict"] == "deterministic_gain"


def _write_tax(tmp_path: Path, name: str, rows: list[tuple[str, str]]) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps({"rows": [{"instance_id": iid, "tier": t} for iid, t in rows]}))
    return p


def test_main_exit_code_zero_on_clean_shift(tmp_path: Path):
    a1 = _write_tax(tmp_path, "a1.json", [("i", "plausible")])
    a2 = _write_tax(tmp_path, "a2.json", [("i", "plausible")])
    a3 = _write_tax(tmp_path, "a3.json", [("i", "plausible")])
    b1 = _write_tax(tmp_path, "b1.json", [("i", "strong")])
    b2 = _write_tax(tmp_path, "b2.json", [("i", "strong")])
    b3 = _write_tax(tmp_path, "b3.json", [("i", "strong")])
    rc = main(["--cycle-a", "v1", str(a1), str(a2), str(a3),
               "--cycle-b", "v2", str(b1), str(b2), str(b3)])
    assert rc == 0


def test_main_exit_code_one_when_any_deterministic_loss(tmp_path: Path):
    a1 = _write_tax(tmp_path, "a1.json", [("i", "strong")])
    a2 = _write_tax(tmp_path, "a2.json", [("i", "strong")])
    a3 = _write_tax(tmp_path, "a3.json", [("i", "strong")])
    b1 = _write_tax(tmp_path, "b1.json", [("i", "empty_patch")])
    b2 = _write_tax(tmp_path, "b2.json", [("i", "empty_patch")])
    b3 = _write_tax(tmp_path, "b3.json", [("i", "empty_patch")])
    rc = main(["--cycle-a", "v1", str(a1), str(a2), str(a3),
               "--cycle-b", "v2", str(b1), str(b2), str(b3)])
    assert rc == 1


def test_main_snapshot_jsonl_one_row_per_instance(tmp_path: Path):
    a1 = _write_tax(tmp_path, "a1.json", [("i", "strong"), ("j", "empty_patch")])
    a2 = _write_tax(tmp_path, "a2.json", [("i", "strong"), ("j", "empty_patch")])
    a3 = _write_tax(tmp_path, "a3.json", [("i", "strong"), ("j", "empty_patch")])
    b1 = _write_tax(tmp_path, "b1.json", [("i", "strong"), ("j", "strong")])
    b2 = _write_tax(tmp_path, "b2.json", [("i", "strong"), ("j", "strong")])
    b3 = _write_tax(tmp_path, "b3.json", [("i", "strong"), ("j", "strong")])
    snap = tmp_path / "snap.jsonl"
    main(["--cycle-a", "v1", str(a1), str(a2), str(a3),
          "--cycle-b", "v2", str(b1), str(b2), str(b3),
          "--snapshot-out", str(snap)])
    lines = snap.read_text().splitlines()
    assert len(lines) == 2
    rows = [json.loads(l) for l in lines]
    by_id = {r["instance_id"]: r for r in rows}
    assert by_id["i"]["verdict"] == "byte_identical"
    assert by_id["j"]["verdict"] == "deterministic_gain"
    assert by_id["i"]["label_a"] == "v1"
    assert by_id["i"]["label_b"] == "v2"


def test_main_returns_2_on_missing_input(tmp_path: Path):
    a1 = _write_tax(tmp_path, "a1.json", [("i", "strong")])
    rc = main(["--cycle-a", "v1", str(a1),
               "--cycle-b", "v2", str(tmp_path / "nonexistent.json")])
    assert rc == 2
