"""Tests for benchmarks/swebench/compare.py — paired McNemar analysis."""

from __future__ import annotations

from benchmarks.swebench.compare import (
    PairedCheckpoint,
    _mcnemar_continuity_corrected,
    _wilson_ci_pp,
    compare,
)


def _cp(name: str, **passed) -> PairedCheckpoint:
    return PairedCheckpoint(name=name, instance_passed=dict(passed))


def test_compare_basic_no_change():
    pre = _cp("pre", a=True, b=True, c=False, d=False)
    post = _cp("post", a=True, b=True, c=False, d=False)
    cmp = compare(pre, post)
    assert cmp.n == 4
    assert cmp.pre_pass == 2
    assert cmp.post_pass == 2
    assert cmp.delta_pp == 0
    assert cmp.mcnemar_b == 0
    assert cmp.mcnemar_c == 0
    assert cmp.mcnemar_p == 1.0
    assert cmp.wins == []
    assert cmp.losses == []


def test_compare_pure_improvement():
    """Every pre-fail flips to post-pass."""
    pre = _cp("pre", a=True, b=False, c=False, d=False)
    post = _cp("post", a=True, b=True, c=True, d=True)
    cmp = compare(pre, post)
    assert cmp.delta_pp == 75
    assert cmp.mcnemar_b == 0
    assert cmp.mcnemar_c == 3
    # 3 conversions all in one direction → low-n exact binomial
    assert cmp.mcnemar_p < 0.5
    assert set(cmp.wins) == {"b", "c", "d"}
    assert cmp.losses == []


def test_compare_pure_regression():
    pre = _cp("pre", a=True, b=True, c=True, d=False)
    post = _cp("post", a=False, b=False, c=False, d=False)
    cmp = compare(pre, post)
    assert cmp.mcnemar_b == 3
    assert cmp.mcnemar_c == 0
    assert cmp.delta_pp == -75
    assert set(cmp.losses) == {"a", "b", "c"}
    assert cmp.wins == []


def test_compare_drops_unshared_instances():
    pre = _cp("pre", a=True, b=False, c=True)
    post = _cp("post", a=False, b=False, d=True)  # c missing, d new
    cmp = compare(pre, post)
    # Only a, b are shared
    assert cmp.n == 2


def test_mcnemar_zero_discordant_returns_p1():
    assert _mcnemar_continuity_corrected(0, 0) == 1.0


def test_mcnemar_large_n_uses_chi_square():
    # 50 vs 0 discordant pairs — overwhelming evidence
    p = _mcnemar_continuity_corrected(50, 0)
    assert p < 0.001


def test_mcnemar_balanced_returns_high_p():
    # Equal flips both ways → no evidence of shift
    p = _mcnemar_continuity_corrected(20, 20)
    assert p > 0.5


def test_wilson_ci_zero_n():
    low, high = _wilson_ci_pp(0.0, 0)
    assert (low, high) == (0.0, 0.0)


def test_wilson_ci_extreme_proportions():
    """Proportion 0/N — CI's lower bound should be 0%."""
    low, high = _wilson_ci_pp(0.0, 100)
    assert 0.0 <= low <= 1.0
    assert high < 5.0  # 95% CI upper bound for 0/100 is around 3.6%
