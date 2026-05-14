"""Tests for scripts/compare_v110.py — v1.10.1 patch_len_delta annotation.

The annotate_patch_len_deltas helper is pure (no I/O, no env) — covers
the row-mutation path that downstream analyzers (analyze_v110_harness.py)
rely on to detect the silent same_tier_docker_demotion class.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.compare_v110 import (
    annotate_patch_len_deltas,
    compute_first_correct_file_touch,
    parse_gold_target_files,
)


def test_patch_len_delta_computed_from_baseline():
    """Target rows get patch_len_delta = target.patch_len - baseline.patch_len."""
    target = {
        "i_shrank": {"tier": "wrong_target", "patch_len": 1659},
        "i_grew": {"tier": "plausible", "patch_len": 1200},
        "i_same": {"tier": "strong", "patch_len": 500},
    }
    baseline = {
        "i_shrank": {"tier": "wrong_target", "patch_len": 3345},
        "i_grew": {"tier": "wrong_location", "patch_len": 800},
        "i_same": {"tier": "strong", "patch_len": 500},
    }
    annotate_patch_len_deltas(target, baseline)
    assert target["i_shrank"]["prior_patch_len"] == 3345
    assert target["i_shrank"]["patch_len_delta"] == -1686  # sphinx-10673 archetype
    assert target["i_grew"]["patch_len_delta"] == 400
    assert target["i_same"]["patch_len_delta"] == 0


def test_patch_len_delta_none_when_baseline_missing():
    """Instance present in target but not in baseline gets None deltas
    (cross-arm-coverage gap; comparing different subsets is not an error).
    """
    target = {"new_instance": {"tier": "strong", "patch_len": 500}}
    baseline: dict = {}
    annotate_patch_len_deltas(target, baseline)
    assert target["new_instance"]["prior_patch_len"] is None
    assert target["new_instance"]["patch_len_delta"] is None


def test_patch_len_delta_zero_when_patches_identical():
    """Identical patches across cycles → delta == 0 (not None)."""
    target = {"i": {"tier": "strong", "patch_len": 1000}}
    baseline = {"i": {"tier": "strong", "patch_len": 1000}}
    annotate_patch_len_deltas(target, baseline)
    assert target["i"]["patch_len_delta"] == 0
    assert target["i"]["prior_patch_len"] == 1000


def test_patch_len_delta_detects_same_tier_shrinkage():
    """The use-case that motivated v1.10.1: inspector tier UNCHANGED but
    patch shrank. The annotation lets downstream analyzers detect this
    even though the inspector taxonomy would treat the rows as identical.
    sphinx-doc__sphinx-10673 is the founding instance."""
    target = {"sphinx-doc__sphinx-10673": {"tier": "wrong_target", "patch_len": 1659}}
    baseline = {"sphinx-doc__sphinx-10673": {"tier": "wrong_target", "patch_len": 3345}}
    annotate_patch_len_deltas(target, baseline)
    row = target["sphinx-doc__sphinx-10673"]
    assert row["tier"] == baseline["sphinx-doc__sphinx-10673"]["tier"], "same_tier precondition"
    assert row["patch_len_delta"] < 0, "shrinkage signature: negative delta on same tier"
    assert row["patch_len_delta"] == -1686


# --- first_correct_file_touch (v1.10.1 substrate for v1.11) ---------------


def test_parse_gold_target_files_single_file():
    diff = (
        "diff --git a/src/luxe/agents/loop.py b/src/luxe/agents/loop.py\n"
        "index abc..def 100644\n"
        "--- a/src/luxe/agents/loop.py\n"
        "+++ b/src/luxe/agents/loop.py\n"
        "@@ -1 +1 @@\n"
        "-old\n+new\n"
    )
    assert parse_gold_target_files(diff) == ["src/luxe/agents/loop.py"]


def test_parse_gold_target_files_multifile_sorted_unique():
    diff = (
        "diff --git a/b/x.py b/b/x.py\n"
        "@@ -1 +1 @@\n"
        "-a\n+b\n"
        "diff --git a/a/y.py b/a/y.py\n"
        "@@ -1 +1 @@\n"
        "-c\n+d\n"
        "diff --git a/b/x.py b/b/x.py\n"  # duplicate header
        "@@ -2 +2 @@\n"
        "-e\n+f\n"
    )
    assert parse_gold_target_files(diff) == ["a/y.py", "b/x.py"]


def test_parse_gold_target_files_empty():
    assert parse_gold_target_files("") == []
    assert parse_gold_target_files(None) == []  # tolerates None


def test_first_correct_file_touch_before_write_and_intervention():
    events = [
        {"kind": "tool_call", "phase": "main", "step": 0,
         "name": "read_file", "path": "wrong.py"},
        {"kind": "tool_call", "phase": "main", "step": 1,
         "name": "read_file", "path": "src/target.py"},
        {"kind": "tool_call", "phase": "main", "step": 2,
         "name": "edit_file", "path": "src/target.py"},
    ]
    result = compute_first_correct_file_touch(events, ["src/target.py"])
    assert result["first_correct_file_touch_step"] == 1
    assert result["correct_touch_before_first_write"] is True
    assert result["correct_touch_relative_to_intervention"] == "before"


def test_first_correct_file_touch_after_intervention():
    """Locus-failure signature: the correct file is touched only AFTER
    an intervention has fired. This is the bucket we expect to
    underperform on Docker — the model needed pressure to find the
    right file, suggesting the trajectory wasn't on-target before."""
    events = [
        {"kind": "tool_call", "phase": "main", "step": 0,
         "name": "read_file", "path": "wrong.py"},
        {"kind": "tool_call", "phase": "main", "step": 1,
         "name": "read_file", "path": "also_wrong.py"},
        {"kind": "early_bail_fired", "step": 4},
        {"kind": "tool_call", "phase": "main", "step": 5,
         "name": "read_file", "path": "src/target.py"},
        {"kind": "tool_call", "phase": "main", "step": 6,
         "name": "edit_file", "path": "src/target.py"},
    ]
    result = compute_first_correct_file_touch(events, ["src/target.py"])
    assert result["first_correct_file_touch_step"] == 5
    assert result["correct_touch_before_first_write"] is True  # touched at step 5, wrote at step 6
    assert result["correct_touch_relative_to_intervention"] == "after"


def test_first_correct_file_touch_never_touched():
    """Trajectory never reads or writes the gold target file. Expected
    to be a high-volume bucket on Docker-failed instances."""
    events = [
        {"kind": "tool_call", "phase": "main", "step": 0,
         "name": "read_file", "path": "wrong.py"},
        {"kind": "tool_call", "phase": "main", "step": 1,
         "name": "edit_file", "path": "wrong.py"},
    ]
    result = compute_first_correct_file_touch(events, ["src/target.py"])
    assert result["first_correct_file_touch_step"] is None
    assert result["correct_touch_before_first_write"] is False
    assert result["correct_touch_relative_to_intervention"] == "none"


def test_first_correct_file_touch_empty_gold_files():
    """Edge case: gold patch parsed to no files (malformed diff or empty).
    Metric defaults to 'none' / False / None."""
    events = [{"kind": "tool_call", "phase": "main", "step": 0,
               "name": "read_file", "path": "anything.py"}]
    result = compute_first_correct_file_touch(events, [])
    assert result["first_correct_file_touch_step"] is None
    assert result["correct_touch_before_first_write"] is False
    assert result["correct_touch_relative_to_intervention"] == "none"


def test_first_correct_file_touch_skips_events_without_path():
    """v1.10 added `path` to tool_call events. Older traces lack the field;
    the helper must skip those entries (returning 'none' if no usable
    tool_call ever lands on a gold file)."""
    events = [
        {"kind": "tool_call", "phase": "main", "step": 0, "name": "read_file"},
        {"kind": "tool_call", "phase": "main", "step": 1, "name": "read_file"},
    ]
    result = compute_first_correct_file_touch(events, ["src/target.py"])
    assert result["first_correct_file_touch_step"] is None


def test_first_correct_file_touch_before_intervention_when_none_fired():
    """If no intervention has fired by the time the gold file is touched,
    the metric reports 'before' (the trajectory found the target on its
    own). Distinguishes "before because no intervention" from "after"."""
    events = [
        {"kind": "tool_call", "phase": "main", "step": 0,
         "name": "read_file", "path": "src/target.py"},
    ]
    result = compute_first_correct_file_touch(events, ["src/target.py"])
    assert result["first_correct_file_touch_step"] == 0
    assert result["correct_touch_relative_to_intervention"] == "before"
