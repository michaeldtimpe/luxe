"""Tests for the deterministic, non-model conversation summarizer."""

from __future__ import annotations

import pytest

from luxe.chat.summarize import SUMMARIZER_VERSION, fold_history


def test_empty_history_is_empty_string():
    assert fold_history([]) == ""


def test_single_turn_kept_verbatim():
    out = fold_history([("hello there", "hi, how can I help?")])
    assert "[user] hello there" in out
    assert "[assistant] hi, how can I help?" in out
    assert "truncated" not in out


def test_recent_turns_verbatim_older_truncated():
    long_user = "x" * 1000
    turns = [
        (long_user, "old answer " * 100),  # older → truncated
        ("recent q", "recent a"),  # within keep_recent
    ]
    out = fold_history(turns, keep_recent=1, older_cap=50, budget_chars=100_000)
    assert "[truncated]" in out  # the old turn was capped
    assert "[user] recent q" in out  # the recent turn is verbatim
    assert "[assistant] recent a" in out


def test_deterministic_for_fixed_input():
    turns = [("a", "b"), ("c", "d"), ("e", "f")]
    a = fold_history(turns)
    b = fold_history(turns)
    assert a == b


def test_budget_drops_oldest_and_marks_elision():
    # Many older turns, tiny budget → oldest dropped, elision marker present,
    # recent tail preserved.
    turns = [(f"user {i}", f"assistant {i} " + "z" * 200) for i in range(20)]
    out = fold_history(turns, keep_recent=2, older_cap=100, budget_chars=400)
    assert "older turns elided" in out
    # the recent tail must survive
    assert "[user] user 19" in out
    assert "[user] user 18" in out
    # an early turn must have been dropped
    assert "[user] user 0" not in out
    # the budgeted fold is meaningfully smaller than folding everything
    unbudgeted = fold_history(turns, keep_recent=2, older_cap=100, budget_chars=10**9)
    assert len(out) < len(unbudgeted)


def test_recent_tail_never_dropped_even_if_over_budget():
    # Even a microscopic budget keeps the verbatim recent tail.
    turns = [(f"u{i}", "a" * 500) for i in range(5)]
    out = fold_history(turns, keep_recent=2, older_cap=50, budget_chars=10)
    assert "[user] u4" in out
    assert "[user] u3" in out


def test_unknown_version_raises():
    with pytest.raises(ValueError):
        fold_history([("a", "b")], version="trunc-v999")


def test_version_constant_is_pinned():
    assert SUMMARIZER_VERSION == "trunc-v1"
