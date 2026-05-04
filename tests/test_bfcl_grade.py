"""Tests for benchmarks/bfcl/grade.py — function-call grader.

PRELIMINARY (2026-05-03). Validates simplified BFCL grading logic against
synthetic ground-truth shapes derived from the bfcl_eval data layout.
"""

from __future__ import annotations

from benchmarks.bfcl.grade import (
    GradeResult,
    _call_matches_gt_entry,
    _value_matches,
    grade,
    grade_irrelevance,
    grade_parallel,
    grade_simple,
)


def test_value_matches_exact():
    assert _value_matches(5, [5, 10]) is True
    assert _value_matches("hello", ["hello", ""]) is True


def test_value_matches_str_to_number():
    assert _value_matches(5, ["5"]) is True  # str gt, int actual
    assert _value_matches("5", [5]) is True  # int gt, str actual


def test_value_matches_no_match():
    assert _value_matches(5, [10, 20]) is False
    assert _value_matches("hello", ["world"]) is False


def test_call_matches_gt_entry_basic():
    gt = {"add": {"a": [1, 2], "b": [3]}}
    assert _call_matches_gt_entry("add", {"a": 1, "b": 3}, gt) is True
    assert _call_matches_gt_entry("add", {"a": 2, "b": 3}, gt) is True
    assert _call_matches_gt_entry("add", {"a": 5, "b": 3}, gt) is False
    assert _call_matches_gt_entry("subtract", {"a": 1, "b": 3}, gt) is False


def test_call_matches_optional_arg_omitted():
    gt = {"f": {"required": [1], "optional": ["", "default"]}}
    # Omitting an arg whose allowed list contains "" is OK.
    assert _call_matches_gt_entry("f", {"required": 1}, gt) is True
    # Providing it is also OK.
    assert _call_matches_gt_entry("f", {"required": 1, "optional": "default"}, gt) is True
    # But providing a wrong value isn't.
    assert _call_matches_gt_entry("f", {"required": 1, "optional": "wrong"}, gt) is False


def test_grade_simple_matches_one_call():
    gt = [{"add": {"a": [1], "b": [2]}}]
    res = grade_simple([("add", {"a": 1, "b": 2})], gt)
    assert res.passed is True
    assert res.actual_calls == 1


def test_grade_simple_no_call_fails():
    gt = [{"add": {"a": [1], "b": [2]}}]
    res = grade_simple([], gt)
    assert res.passed is False
    assert "no_tool_call" in res.reason


def test_grade_simple_too_many_calls_fails():
    gt = [{"add": {"a": [1], "b": [2]}}]
    res = grade_simple([("add", {"a": 1, "b": 2}), ("add", {"a": 1, "b": 2})], gt)
    assert res.passed is False
    assert "expected_1" in res.reason


def test_grade_simple_picks_any_gt_entry():
    """Multiple-choice GT — any entry is acceptable."""
    gt = [
        {"add": {"a": [1], "b": [2]}},
        {"sum": {"x": [3]}},
    ]
    assert grade_simple([("add", {"a": 1, "b": 2})], gt).passed is True
    assert grade_simple([("sum", {"x": 3})], gt).passed is True


def test_grade_parallel_set_match():
    gt = [
        {"f": {"x": [1]}},
        {"g": {"y": [2]}},
    ]
    # Order doesn't matter
    assert grade_parallel(
        [("g", {"y": 2}), ("f", {"x": 1})], gt
    ).passed is True


def test_grade_parallel_count_mismatch():
    gt = [{"f": {"x": [1]}}, {"g": {"y": [2]}}]
    res = grade_parallel([("f", {"x": 1})], gt)
    assert res.passed is False
    assert "expected_2" in res.reason


def test_grade_parallel_one_unmatched_call_fails():
    gt = [{"f": {"x": [1]}}, {"g": {"y": [2]}}]
    res = grade_parallel(
        [("f", {"x": 1}), ("h", {"z": 3})], gt
    )
    assert res.passed is False
    assert "unmatched" in res.reason


def test_grade_irrelevance_no_call_passes():
    res = grade_irrelevance([])
    assert res.passed is True


def test_grade_irrelevance_call_fails():
    res = grade_irrelevance([("calculator", {"expr": "1+1"})])
    assert res.passed is False
    assert "calculator" in res.reason


def test_grade_dispatch_routes_by_category():
    gt = [{"f": {"x": [1]}}]
    assert grade("simple_python", [("f", {"x": 1})], gt).passed is True
    assert grade("multiple", [("f", {"x": 1})], gt).passed is True
    assert grade("parallel", [("f", {"x": 1})], gt).passed is True
    assert grade("parallel_multiple", [("f", {"x": 1})], gt).passed is True
    assert grade("irrelevance", [], None).passed is True


def test_grade_unsupported_category():
    res = grade("nonexistent", [], None)
    assert res.passed is False
    assert "unsupported" in res.reason
