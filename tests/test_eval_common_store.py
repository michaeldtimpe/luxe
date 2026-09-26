"""benchmarks/_eval_common/store.py + the runners that use it.

The extended-benchmark runners each hand-rolled resume: any file at the item
path was reused (another model's or variant's included), summaries globbed
the whole directory (a --limit run reported over a prior full run's items),
and a backend exception was scored and cached (gsm8k extracted numbers out
of the error text)."""
from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks._eval_common.choices import pick_choice
from benchmarks._eval_common.store import ResultStore


# --- the store ------------------------------------------------------------

def test_cache_hit_requires_same_model_and_variant(tmp_path):
    a = ResultStore(tmp_path, model="m1", variant="minimal")
    a.put("item_00000", {"correct": True}, "q0")
    assert a.get("item_00000", "q0")["correct"] is True
    assert ResultStore(tmp_path, model="m2", variant="minimal").get("item_00000", "q0") is None
    assert ResultStore(tmp_path, model="m1", variant="full").get("item_00000", "q0") is None


def test_fingerprint_mismatch_is_not_a_hit(tmp_path):
    s = ResultStore(tmp_path, model="m")
    s.put("q_0003", {"correct": True}, "What is 2+2?")
    assert s.get("q_0003", "A different question") is None


def test_legacy_unstamped_record_is_not_reused(tmp_path):
    (tmp_path / "item_00000.json").write_text(json.dumps({"correct": True}))
    assert ResultStore(tmp_path, model="m").get("item_00000") is None


def test_infra_error_is_never_a_hit_and_never_graded(tmp_path):
    s = ResultStore(tmp_path, model="m")
    s.put_infra_error("item_00000", "ConnectError: refused")
    assert s.get("item_00000") is None
    got = s.collect(["item_00000"])
    assert got.records == [] and got.infra_errors == 1


def test_collect_reads_only_the_selected_ids(tmp_path):
    s = ResultStore(tmp_path, model="m")
    for i in range(5):
        s.put(f"item_{i:05d}", {"correct": True}, str(i))
    got = s.collect([("item_00000", "0"), ("item_00001", "1")])
    assert len(got.records) == 2


def test_no_resume_disables_hits(tmp_path):
    ResultStore(tmp_path, model="m").put("x", {"correct": True})
    assert ResultStore(tmp_path, model="m", resume=False).get("x") is None


def test_pick_choice_returns_none_when_no_letter_is_in_top_k():
    inf = float("-inf")
    assert pick_choice({"A": inf, "B": inf, "C": inf, "D": inf}, "ABCD") is None
    assert pick_choice({"A": inf, "B": -1.2, "C": -0.3, "D": inf}, "ABCD") == "C"
