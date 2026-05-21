"""Smoke test for scripts/split_cohort_snapshot.py — Stage 2 → Stage 3
closed loop wiring."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from luxe.agents.cohort_priors import load_prior
from scripts.split_cohort_snapshot import main as split_main


def test_split_writes_one_file_per_instance_and_loads_back(tmp_path: Path):
    # Write a synthetic snapshot JSONL (the shape cohort_shift_3x3.py emits).
    snap = tmp_path / "snapshot.jsonl"
    rows = [
        {"instance_id": "repo__bug-1", "verdict": "byte_identical",
         "label_a": "v1.10.4", "label_b": "v1.10.5",
         "tiers_a": ["strong"] * 3, "tiers_b": ["strong"] * 3,
         "rank_delta": 0.0},
        {"instance_id": "repo__bug-2", "verdict": "deterministic_gain",
         "label_a": "v1.10.4", "label_b": "v1.10.5",
         "tiers_a": ["empty_patch"] * 3, "tiers_b": ["wrong_location"] * 3,
         "rank_delta": -3.0},
    ]
    snap.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    out_dir = tmp_path / "cohort-history"
    rc = split_main(["--snapshot", str(snap), "--out-dir", str(out_dir)])
    assert rc == 0
    assert (out_dir / "repo__bug-1.json").is_file()
    assert (out_dir / "repo__bug-2.json").is_file()

    # Stage 3 loader resolves both.
    p1 = load_prior("repo__bug-1", base_dir=out_dir)
    p2 = load_prior("repo__bug-2", base_dir=out_dir)
    assert p1 is not None and p1["verdict"] == "byte_identical"
    assert p2 is not None and p2["verdict"] == "deterministic_gain"


def test_split_handles_empty_snapshot(tmp_path: Path):
    snap = tmp_path / "empty.jsonl"
    snap.write_text("")
    out_dir = tmp_path / "out"
    rc = split_main(["--snapshot", str(snap), "--out-dir", str(out_dir)])
    assert rc == 0
    assert out_dir.is_dir()
    assert list(out_dir.iterdir()) == []


def test_split_returns_2_on_missing_snapshot(tmp_path: Path):
    rc = split_main(["--snapshot", str(tmp_path / "nonexistent.jsonl"),
                     "--out-dir", str(tmp_path / "out")])
    assert rc == 2
