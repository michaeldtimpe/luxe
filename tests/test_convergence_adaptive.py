"""v1.11 Phase 1 — adaptive-policy substrate invariant tests.

Covers the 4 invariants pinned in agents.sdd:
  - bias-not-lock: no signal combination zeros out or saturates modulation
  - slew-rate limit: per-step intensity delta bounded
  - score_log ownership: convergence.py never mutates the passed sequence
  - disable-equivalence: covered by tests/test_loop_*.py running with
    LUXE_ADAPTIVE_POLICY unset (default behavior unchanged); this file
    adds explicit per-component verification

Plus per-signal ablation + pure-function tests for the Group A signals.
"""
from __future__ import annotations

import random

import pytest

from luxe.agents.convergence import (
    AdaptiveState,
    _DEFAULT_MAX_DELTA,
    _INTENSITY_MAX,
    _INTENSITY_MIN,
    _INTENSITY_NEUTRAL,
    _NO_WRITE_BIAS_THRESHOLD,
    _SCORE_TREND_WINDOW,
    apply_slew_rate,
    bias_to_modulation,
    compute_intervention_bias,
    compute_within_run_state,
    consecutive_no_write_steps,
    score_trajectory_trend,
)


# --- consecutive_no_write_steps ------------------------------------------

def test_no_write_count_zero_on_empty_history():
    assert consecutive_no_write_steps([]) == 0


def test_no_write_count_includes_all_non_write_tail():
    history = [
        {"name": "read_file", "path": "a.py"},
        {"name": "grep", "path": "b.py"},
        {"name": "bash", "path": None},
    ]
    assert consecutive_no_write_steps(history) == 3


def test_no_write_count_resets_after_write():
    history = [
        {"name": "read_file", "path": "a.py"},
        {"name": "write_file", "path": "a.py"},
        {"name": "read_file", "path": "b.py"},
    ]
    assert consecutive_no_write_steps(history) == 1


def test_no_write_count_zero_when_last_is_write():
    history = [
        {"name": "read_file", "path": "a.py"},
        {"name": "edit_file", "path": "a.py"},
    ]
    assert consecutive_no_write_steps(history) == 0


def test_no_write_count_only_counts_trailing_run():
    history = [
        {"name": "read_file"} for _ in range(5)
    ] + [
        {"name": "write_file"},
    ] + [
        {"name": "read_file"} for _ in range(3)
    ]
    # 5 reads before the write don't count; 3 reads after do.
    assert consecutive_no_write_steps(history) == 3


# --- score_trajectory_trend ----------------------------------------------

def test_score_trend_zero_when_insufficient_data():
    assert score_trajectory_trend([]) == 0.0
    assert score_trajectory_trend([0.5]) == 0.0


def test_score_trend_positive_when_monotonic_increasing():
    assert score_trajectory_trend([0.1, 0.2, 0.3, 0.4, 0.5]) == 1.0


def test_score_trend_negative_when_monotonic_decreasing():
    assert score_trajectory_trend([0.5, 0.4, 0.3, 0.2, 0.1]) == -1.0


def test_score_trend_zero_when_flat():
    assert score_trajectory_trend([0.3, 0.3, 0.3, 0.3, 0.3]) == 0.0


def test_score_trend_uses_window_endpoints_not_intermediate():
    # First and last in the window determine direction; jitter in middle
    # is ignored by design (robust to noise).
    vals = [0.1, 0.9, 0.1, 0.9, 0.5]
    assert score_trajectory_trend(vals) == 1.0  # 0.5 > 0.1


def test_score_trend_only_uses_last_window():
    early = [0.9, 0.8, 0.7]
    recent = [0.1, 0.2, 0.3, 0.4, 0.5]
    # Default window 5 → only `recent` contributes; trend should be +1.
    assert score_trajectory_trend(early + recent) == 1.0


# --- compute_within_run_state composition + ablation --------------------

def test_within_run_state_composes_both_signals():
    state = compute_within_run_state(
        score_log=[0.1, 0.2, 0.3, 0.4, 0.5],
        tool_history=[{"name": "read_file"}] * 10,
        step=10,
    )
    assert state.consecutive_no_write == 10
    assert state.score_trend == 1.0
    assert state.step == 10
    assert state.score_log_len == 5


def test_within_run_state_ablation_no_write_off_returns_none():
    state = compute_within_run_state(
        score_log=[0.1, 0.5],
        tool_history=[{"name": "read_file"}] * 4,
        step=4,
        no_write_enabled=False,
    )
    assert state.consecutive_no_write is None
    assert state.score_trend is not None


def test_within_run_state_ablation_score_trend_off_returns_none():
    state = compute_within_run_state(
        score_log=[0.1, 0.5],
        tool_history=[{"name": "read_file"}] * 4,
        step=4,
        score_trend_enabled=False,
    )
    assert state.consecutive_no_write is not None
    assert state.score_trend is None


def test_within_run_state_ablation_both_off_returns_nones():
    state = compute_within_run_state(
        score_log=[0.1, 0.5],
        tool_history=[{"name": "read_file"}] * 4,
        step=4,
        no_write_enabled=False,
        score_trend_enabled=False,
    )
    assert state.consecutive_no_write is None
    assert state.score_trend is None


def test_within_run_state_is_frozen_dataclass():
    state = compute_within_run_state([], [], 0)
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        state.step = 99  # type: ignore[misc]


def test_within_run_state_does_not_mutate_inputs():
    """score_log ownership invariant — convergence.py never mutates the deque."""
    log = [0.1, 0.2, 0.3]
    hist = [{"name": "read_file"}, {"name": "write_file"}]
    log_copy = list(log)
    hist_copy = list(hist)
    compute_within_run_state(log, hist, 0)
    assert log == log_copy
    assert hist == hist_copy


# --- compute_intervention_bias + bias-not-lock invariant -----------------

def test_bias_no_write_retired_write_pressure_pinned_at_zero():
    """v1.11 Phase B — no_write → write_pressure/early_bail bias RETIRED
    (Phase A: non-selective). The keys exist but are pinned at 0.0 regardless
    of how deep the no-write streak runs, so write_pressure stays neutral."""
    for nw in (3, _NO_WRITE_BIAS_THRESHOLD + 2, 999):
        state = AdaptiveState(
            step=12, consecutive_no_write=nw, score_trend=0.0, score_log_len=5)
        bias = compute_intervention_bias(state)
        assert bias["write_pressure"] == 0.0, f"no_write={nw} should not bias wp"
        assert bias["early_bail"] == 0.0


def test_bias_suppresses_soft_anchor_when_converging():
    """trend > 0 → back off commitment pressure (ungated; always safe)."""
    state = AdaptiveState(
        step=5, consecutive_no_write=2, score_trend=1.0, score_log_len=5,
        convergence_score=0.05)
    bias = compute_intervention_bias(state)
    assert bias.get("soft_anchor", 0) < 0


def test_bias_promotes_soft_anchor_on_confirmed_collapse():
    """trend <= 0 AND conv < LOW AND step >= _COLLAPSE_MIN_STEP → positive
    soft_anchor bias (the score<LOW band-response promotion signal)."""
    state = AdaptiveState(
        step=7, consecutive_no_write=2, score_trend=-1.0, score_log_len=5,
        convergence_score=0.05)
    bias = compute_intervention_bias(state)
    assert bias.get("soft_anchor", 0) > 0


def test_bias_no_soft_anchor_promotion_before_collapse_min_step():
    """The same collapse signature BEFORE _COLLAPSE_MIN_STEP must NOT promote
    — Phase A showed empties only separate from preserves at step >= 7."""
    state = AdaptiveState(
        step=5, consecutive_no_write=2, score_trend=-1.0, score_log_len=5,
        convergence_score=0.05)
    bias = compute_intervention_bias(state)
    assert bias.get("soft_anchor", 0.0) == 0.0


def test_bias_no_soft_anchor_promotion_when_above_low_band():
    """Confirmed-collapse step but convergence already above LOW (climbing
    toward commit) must NOT promote — it is not the empty signature."""
    state = AdaptiveState(
        step=9, consecutive_no_write=2, score_trend=0.0, score_log_len=5,
        convergence_score=0.20)
    bias = compute_intervention_bias(state)
    assert bias.get("soft_anchor", 0.0) == 0.0


# --- bias-not-lock property test: random states → safe modulation --------

def test_bias_not_lock_invariant_random_states_never_saturate_modulation():
    """For any random AdaptiveState, the resulting bias → modulation
    must produce a value STRICTLY inside [_INTENSITY_MIN, _INTENSITY_MAX].
    This is the core bias-not-lock invariant from agents.sdd.
    """
    rng = random.Random(20260520)
    for _ in range(500):
        state = AdaptiveState(
            step=rng.randint(0, 100),
            consecutive_no_write=(rng.choice([None, rng.randint(0, 50)])),
            score_trend=rng.choice([None, -1.0, 0.0, 1.0]),
            score_log_len=rng.randint(0, 50),
            convergence_score=rng.choice([None, 0.0, 0.05, 0.1, 0.4, 1.0]),
        )
        bias = compute_intervention_bias(state)
        for kind, b in bias.items():
            assert -1.0 <= b <= 1.0, f"bias for {kind} out of range: {b}"
            mod = bias_to_modulation(b)
            # STRICT bounds: never exactly _INTENSITY_MIN (would gate) or
            # exactly _INTENSITY_MAX (would saturate).
            assert _INTENSITY_MIN < mod < _INTENSITY_MAX, (
                f"modulation {mod} hit bound for {kind} bias={b}"
            )


def test_bias_to_modulation_neutral_for_zero_bias():
    assert bias_to_modulation(0.0) == pytest.approx(_INTENSITY_NEUTRAL)


def test_bias_to_modulation_strictly_below_max_for_max_bias():
    assert bias_to_modulation(1.0) < _INTENSITY_MAX


def test_bias_to_modulation_strictly_above_min_for_min_bias():
    assert bias_to_modulation(-1.0) > _INTENSITY_MIN


# --- slew-rate limit ------------------------------------------------------

def test_slew_rate_allows_change_within_max_delta():
    assert apply_slew_rate(prev_modulation=1.0, target_modulation=1.2, max_delta=0.3) == pytest.approx(1.2)


def test_slew_rate_caps_increase_at_max_delta():
    out = apply_slew_rate(prev_modulation=1.0, target_modulation=1.5, max_delta=0.3)
    assert out == pytest.approx(1.3)


def test_slew_rate_caps_decrease_at_max_delta():
    out = apply_slew_rate(prev_modulation=1.0, target_modulation=0.0, max_delta=0.3)
    assert out == pytest.approx(0.7)


def test_slew_rate_clamps_to_intensity_bounds():
    # Even if max_delta is large, output is clamped to [_INTENSITY_MIN, _INTENSITY_MAX].
    out = apply_slew_rate(prev_modulation=1.4, target_modulation=5.0, max_delta=10.0)
    assert out == _INTENSITY_MAX
    out = apply_slew_rate(prev_modulation=0.2, target_modulation=-5.0, max_delta=10.0)
    assert out == _INTENSITY_MIN


def test_slew_rate_negative_max_delta_treated_as_zero():
    out = apply_slew_rate(prev_modulation=1.0, target_modulation=1.5, max_delta=-0.3)
    assert out == pytest.approx(1.0)


def test_slew_rate_default_max_delta_constant():
    # Sanity check the design-pinned default is 0.3 (matches agents.sdd).
    assert _DEFAULT_MAX_DELTA == 0.3
