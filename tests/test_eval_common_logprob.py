"""Offline unit tests for benchmarks/_eval_common/logprob.py.

The critical property tested here: no token is double-counted across
sliding windows. A sloppy implementation will overcount tokens in the
overlap region and produce a lower perplexity than reality.
"""
from __future__ import annotations

import math

import pytest

from benchmarks._eval_common.logprob import (
    aggregate,
    plan_sliding_windows,
)


class TestPlanSlidingWindows:
    def test_corpus_smaller_than_window(self):
        ws = plan_sliding_windows(total_tokens=10, window=20, stride=10)
        assert len(ws) == 1
        assert ws[0].window_start == 0
        assert ws[0].window_end == 10
        assert ws[0].eval_start == 1
        assert ws[0].eval_end == 10

    def test_corpus_equals_window(self):
        ws = plan_sliding_windows(total_tokens=20, window=20, stride=10)
        assert len(ws) == 1
        assert ws[0].eval_count == 19  # all tokens except token 0

    def test_corpus_just_larger_than_window(self):
        # total=25, window=20, stride=10
        # Window 0: ctx [0,20), eval [1,20) — 19 tokens
        # Window 1: starts at 10, ctx [10,30) clipped to [10,25), eval [20,25) — 5 new
        ws = plan_sliding_windows(total_tokens=25, window=20, stride=10)
        assert len(ws) == 2
        assert ws[0].window_start == 0 and ws[0].window_end == 20
        assert ws[0].eval_start == 1 and ws[0].eval_end == 20
        assert ws[1].window_start == 10 and ws[1].window_end == 25
        assert ws[1].eval_start == 20 and ws[1].eval_end == 25

    def test_no_double_counting(self):
        # Slide a 20-token window across a 100-token corpus with stride=10.
        # Every token from 1..99 should be evaluated exactly once.
        ws = plan_sliding_windows(total_tokens=100, window=20, stride=10)
        evaluated = set()
        for w in ws:
            for tok in range(w.eval_start, w.eval_end):
                assert tok not in evaluated, f"token {tok} counted twice"
                evaluated.add(tok)
        expected = set(range(1, 100))
        assert evaluated == expected, (
            f"missed tokens: {expected - evaluated}; "
            f"extra: {evaluated - expected}"
        )

    def test_stride_equals_window_skips_boundary_tokens(self):
        # When stride==window, windows do not overlap, so the first token
        # of every window past window 0 has no in-window prior context and
        # must be skipped (otherwise its NLL would be computed against an
        # empty context, inflating perplexity). This is a known trade-off
        # of non-overlapping evaluation — set stride < window if you want
        # every token scored.
        ws = plan_sliding_windows(total_tokens=60, window=20, stride=20)
        evaluated = set()
        for w in ws:
            for tok in range(w.eval_start, w.eval_end):
                assert tok not in evaluated
                evaluated.add(tok)
        # Skipped: token 0 (no prior in any window), 20, 40 (first of windows 1,2)
        skipped = {0, 20, 40}
        assert evaluated == set(range(60)) - skipped

    def test_too_small_window_raises(self):
        with pytest.raises(ValueError):
            plan_sliding_windows(total_tokens=10, window=1, stride=1)

    def test_stride_zero_raises(self):
        with pytest.raises(ValueError):
            plan_sliding_windows(total_tokens=10, window=4, stride=0)

    def test_stride_greater_than_window_raises(self):
        with pytest.raises(ValueError):
            plan_sliding_windows(total_tokens=10, window=4, stride=5)

    def test_empty_or_single_token(self):
        assert plan_sliding_windows(total_tokens=0, window=4, stride=2) == []
        assert plan_sliding_windows(total_tokens=1, window=4, stride=2) == []


class TestAggregate:
    def test_basic_perplexity(self):
        # 10 tokens, mean nll = 2.0 ⇒ perplexity = exp(2.0)
        res = aggregate(nll_sum=20.0, token_count=10, num_windows=1)
        assert math.isclose(res.perplexity, math.exp(2.0))
        assert res.token_count == 10
        assert res.num_windows == 1

    def test_zero_token_count_raises(self):
        with pytest.raises(ValueError):
            aggregate(nll_sum=0.0, token_count=0, num_windows=0)

    def test_perplexity_of_uniform_distribution(self):
        # If every token has logprob = -log(V), perplexity should be V.
        V = 50000
        log_v = math.log(V)
        n = 100
        res = aggregate(nll_sum=log_v * n, token_count=n, num_windows=1)
        assert math.isclose(res.perplexity, V)
