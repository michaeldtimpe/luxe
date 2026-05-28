"""Sliding-window perplexity helpers.

Pure functions over abstract logprob sequences — no model, no Backend.
The actual logprob computation happens in MLXDirectBackend; this module
plans the windows and aggregates the result.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class PerplexityWindow:
    """One sliding window over the corpus.

    window_start, window_end:   absolute token indices the caller feeds
                                to the model for context.
    eval_start, eval_end:       absolute token indices whose logprobs
                                contribute to the perplexity sum. Always
                                a contiguous suffix of [window_start, window_end).
    """

    window_start: int
    window_end: int
    eval_start: int
    eval_end: int

    @property
    def context_size(self) -> int:
        return self.window_end - self.window_start

    @property
    def eval_count(self) -> int:
        return self.eval_end - self.eval_start


@dataclass(frozen=True)
class PerplexityResult:
    perplexity: float
    nll_sum: float
    token_count: int
    num_windows: int


def plan_sliding_windows(
    total_tokens: int,
    window: int,
    stride: int,
) -> list[PerplexityWindow]:
    """Plan non-overlapping evaluation across sliding context windows.

    Window 0:    context [0, window),   evaluate logprobs of tokens [1, window).
                 (token 0 has no prior context, so it's skipped.)
    Window i>0:  context [i*stride, i*stride+window),
                 evaluate logprobs of tokens [last_eval_end, window_end).
                 The eval region is exactly the slice of new tokens not
                 already counted, so no token is double-counted.

    If total_tokens <= window, returns a single window covering everything.
    """
    if window < 2:
        raise ValueError("window must be >= 2 (need at least one prior token)")
    if stride < 1 or stride > window:
        raise ValueError(f"stride must be in [1, {window}], got {stride}")
    if total_tokens < 2:
        return []

    if total_tokens <= window:
        return [PerplexityWindow(0, total_tokens, 1, total_tokens)]

    windows = [PerplexityWindow(0, window, 1, window)]
    last_eval_end = window

    i = 1
    while last_eval_end < total_tokens:
        w_start = i * stride
        w_end = min(w_start + window, total_tokens)
        if w_start >= total_tokens:
            break
        eval_start = max(last_eval_end, w_start + 1)
        if eval_start >= w_end:
            i += 1
            continue
        windows.append(PerplexityWindow(w_start, w_end, eval_start, w_end))
        last_eval_end = w_end
        i += 1

    return windows


def aggregate(nll_sum: float, token_count: int, num_windows: int) -> PerplexityResult:
    """Compute perplexity = exp(nll_sum / token_count)."""
    if token_count <= 0:
        raise ValueError("token_count must be positive")
    return PerplexityResult(
        perplexity=math.exp(nll_sum / token_count),
        nll_sum=nll_sum,
        token_count=token_count,
        num_windows=num_windows,
    )
