"""v1.10 — convergence-score primitive for conditional intervention stacking.

The v1.9 cycle's binary `same_file_read_twice` proxy was empirically too
coarse: full-stack PROTECTED v18 strongs but BROKE some plausibles;
gate-only did the inverse. The Phase D A/B at n=75 showed pure
intervention stacking is non-Pareto — each text-level steer has no
awareness of the others' state or of the model's trajectory shape at
fire time.

v1.10 replaces the binary proxy with a smooth convergence score in
[0.0, 1.0] derived from four signals over the recent tool-call history:

  - repeated_same_path_access  — fraction of reads that revisit a path
  - edit_preview_behavior      — diff/grep/preview observed before write
  - localized_grep_density     — fraction of grep targets in the same
                                 directory as recent reads
  - file_entropy_last_K_events — 1 − normalized Shannon entropy over
                                 the path frequency distribution

Intervention intensity scales with the score:
  score < LOW   — diffuse-recon; suppress commitment intervention
  LOW ≤ s < HIGH — standard soft-anchor / early_bail
  score ≥ HIGH  — tighter commitment phrasing

The function is pure and operates over a list of dicts so it can be
unit-tested in isolation against synthetic trajectories. Loop callers
maintain `tool_history` (last K events) and pass it through.

Tool-history entry shape:
    {
      "step": int,
      "name": str,                   # tool name, lowercased
      "path": str | None,            # path arg if extractable, else None
      "key_hash": str | None,        # _call_key SHA-1 prefix (optional)
    }

Path extraction is permissive — the caller can pass whatever path
proxy is available. For tools that don't have a path (e.g. `bash`),
caller can pass `None`; convergence features treat them as neutral.
"""

from __future__ import annotations

import math
import posixpath
from collections import Counter
from typing import Any, Sequence

_READ_TOOLS = frozenset({"read_file"})
_GREP_TOOLS = frozenset({"grep", "bm25_search"})
_PREVIEW_TOOLS = frozenset({"grep", "bm25_search", "git_diff"})
_WRITE_TOOLS = frozenset({"write_file", "edit_file"})


def _dirname(path: str | None) -> str | None:
    if not path:
        return None
    d = posixpath.dirname(path)
    return d or "."


def _normalized_entropy(paths: Sequence[str | None]) -> float:
    """Shannon entropy of the path distribution, normalized to [0, 1]
    where 0 = all paths identical (max convergence) and 1 = all paths
    unique (max diffuseness). Ignores None entries (non-path tools)."""
    real = [p for p in paths if p]
    if not real:
        return 0.0
    counts = Counter(real)
    n = len(real)
    if n <= 1 or len(counts) == 1:
        return 0.0
    h = 0.0
    for c in counts.values():
        p = c / n
        h -= p * math.log2(p)
    h_max = math.log2(min(n, len(counts)))
    return h / h_max if h_max > 0 else 0.0


def repeated_same_path_access(history: Sequence[dict[str, Any]]) -> float:
    """Fraction of read_file calls that hit a path already seen in the
    recent history. 0.0 = all unique reads; 1.0 = every read was a
    repeat. Strong trajectories empirically show ~3× higher rates than
    empties per the v18 distribution mining."""
    reads = [h for h in history if h.get("name") in _READ_TOOLS and h.get("path")]
    if not reads:
        return 0.0
    unique = len({h["path"] for h in reads})
    return 1.0 - (unique / len(reads))


def edit_preview_behavior(history: Sequence[dict[str, Any]]) -> float:
    """1.0 if any write was immediately preceded by a preview/grep/diff
    in the history window; 0.0 otherwise. Models that "look before they
    leap" are typically committing to a known target — a strong
    convergence signal."""
    for i, h in enumerate(history):
        if h.get("name") not in _WRITE_TOOLS:
            continue
        prev = history[i - 1] if i > 0 else None
        if prev and prev.get("name") in _PREVIEW_TOOLS:
            return 1.0
    return 0.0


def localized_grep_density(history: Sequence[dict[str, Any]]) -> float:
    """Fraction of grep/search targets whose directory matches the
    directory of a recent read_file. High values mean searches are
    localized rather than scattered — a convergence signal. Returns
    0.0 if no greps in history or no reads to compare to."""
    greps = [h for h in history if h.get("name") in _GREP_TOOLS]
    reads = [h for h in history if h.get("name") in _READ_TOOLS]
    if not greps or not reads:
        return 0.0
    read_dirs = {_dirname(h.get("path")) for h in reads}
    read_dirs.discard(None)
    if not read_dirs:
        return 0.0
    localized = 0
    counted = 0
    for g in greps:
        g_dir = _dirname(g.get("path"))
        if g_dir is None:
            continue
        counted += 1
        if g_dir in read_dirs:
            localized += 1
    if counted == 0:
        return 0.0
    return localized / counted


def file_entropy_last_K(history: Sequence[dict[str, Any]],
                        k: int = 10) -> float:
    """Convergence component derived from path entropy. Returns
    `1 − normalized_entropy(last K paths)` so higher values mean more
    convergence (model is touching the same paths repeatedly)."""
    window = list(history)[-k:]
    paths = [h.get("path") for h in window]
    return 1.0 - _normalized_entropy(paths)


# v1.10.2 — recent_path_diversity is a TOPOLOGY signal, not a confidence
# scalar. It's used by loop.py's dispatcher to distinguish two trajectory
# shapes that v1.10's `score < LOW` band conflated:
#
#   high diversity + low score  →  true exploration (model still searching
#                                  hypothesis space; matplotlib-14623
#                                  archetype). Fire exploratory variant.
#   low diversity + low score   →  focused-but-uncommitted (model circling
#                                  a candidate but unwilling to act;
#                                  pylint-6528 / sphinx-10323 archetype).
#                                  Fall back to soft_anchor (force commit).
#
# v1.10.1 collateral: both shapes were getting the permissive exploratory
# message, but the second shape interpreted "you may begin attempting"
# as license to keep exploring rather than commit the candidate it had.
#
# Future-work concerns (reviewer note; queued for v1.10.3+ if raw count
# becomes noisy at scale):
#   - repo-size sensitivity: large repos induce broader incidental reads
#   - helper-file fanout: utility files inflate diversity without
#     representing real exploration
#   - tool verbosity: some tools enumerate files implicitly
# Candidates if/when needed: entropy-weighted diversity, per-directory
# semantic diversity, distinct-module count. For v1.10.2 the raw count
# is the minimal lever.
#
# Calibration finding (v1.10.2 diagnostic on real v1.10.1 traces):
# At early_bail fire-time (step=4), diversity ≤ 3 across all three W3
# cases (matplotlib-14623 = 2, pylint-6528 = 2, sphinx-10323 = 1).
# Diversity-at-fire-time CANNOT discriminate true exploration from
# focused circling — the discriminator emerges later (matplotlib-14623
# went on to read 10 total paths, pylint-6528 stopped at 4). Threshold
# set to 2: suppresses only the most-minimal trajectories (≤ 1 distinct
# path), passes everything else through to exploratory. The
# post_exploratory_escalation predicate in loop.py handles the
# focused-circling failure mode.
_DIVERSITY_WINDOW_K = 8
_DIVERSITY_MIN_FOR_EXPLORATORY = 2


def recent_path_diversity(history: Sequence[dict[str, Any]],
                          k: int = _DIVERSITY_WINDOW_K) -> int:
    """Count of DISTINCT paths in the last `k` tool_call history entries.
    Entries without a `path` field are skipped. Returns 0 when no
    path-bearing entries exist in the window.

    Pure function over tool_history; cheap to evaluate per step alongside
    compute_convergence_score.
    """
    window = list(history)[-k:] if k > 0 else list(history)
    paths = [h.get("path") for h in window if h.get("path")]
    return len(set(paths))


# Weights chosen so each signal contributes at most 0.25 to the score;
# convergence is the conjunction of multiple signals (not any one alone).
_DEFAULT_WEIGHTS = {
    "repeated_same_path_access": 0.25,
    "edit_preview_behavior": 0.25,
    "localized_grep_density": 0.25,
    "file_entropy_last_K": 0.25,
}


def compute_convergence_score(
    history: Sequence[dict[str, Any]],
    *,
    weights: dict[str, float] | None = None,
) -> float:
    """Return a smooth convergence score in [0.0, 1.0].

    0.0 = pure diffuse-recon; 1.0 = strongly converged. Linear weighted
    combination of four sub-signals (see module docstring). The
    function is pure — no I/O, no globals, no clock reads — so callers
    can run it on every step without performance concern.

    `history` should contain the agent's tool-call sequence with
    extracted path args. Empty history returns 0.0 (no convergence
    information yet → treat as diffuse / allow standard interventions
    to fire as before).
    """
    if not history:
        return 0.0
    w = weights or _DEFAULT_WEIGHTS
    s = (
        w["repeated_same_path_access"] * repeated_same_path_access(history)
        + w["edit_preview_behavior"] * edit_preview_behavior(history)
        + w["localized_grep_density"] * localized_grep_density(history)
        + w["file_entropy_last_K"] * file_entropy_last_K(history)
    )
    return max(0.0, min(1.0, s))


def extract_path(name: str, args: dict[str, Any]) -> str | None:
    """Permissive path extraction from tool arguments. Returns the
    first available path-like value, or None if the tool's args don't
    contain one. Caller-facing helper so the loop has a single place
    to define "what counts as a path"."""
    if not isinstance(args, dict):
        return None
    for k in ("path", "file_path", "filepath", "filename", "file"):
        v = args.get(k)
        if isinstance(v, str) and v:
            return v
    return None


# ============================================================================
# v1.11 Phase 1 — Group A adaptive-policy signals (substrate; no behavior
# change in Phase 1)
# ============================================================================
#
# Two within-run trajectory signals. Per agents.sdd v1.11 invariants:
#   - bias-not-lock: signals never enable/disable an intervention, only
#     modulate its timing/intensity within [_INTENSITY_MIN, _INTENSITY_MAX]
#   - score_log ownership: loop.py owns the per-step score deque and passes
#     it in; convergence.py is the sole consumer (stateless)
#   - slew-rate limit: per-step modulation change bounded by
#     _DEFAULT_MAX_DELTA (overridable via LUXE_ADAPTIVE_MAX_INTENSITY_DELTA_PER_STEP)
#   - disable-equivalence: when LUXE_ADAPTIVE_POLICY=0 (or unset), none of
#     these functions are called by loop.py — v1.10.5 behavior preserved
#     byte-identical
#
# Phase 1 wires computation + emits an adaptive_state observability event
# from loop.py. It does NOT wire bias into intervention dispatch — that
# happens in Phase 3a (archetype preflight) so any behavior change can be
# isolated under the archetype probe.

from dataclasses import dataclass, field

_SCORE_TREND_WINDOW = 5
_INTENSITY_MIN = 0.0
_INTENSITY_MAX = 1.5
_INTENSITY_NEUTRAL = 1.0
_DEFAULT_MAX_DELTA = 0.3
# Heuristic from v1.10.5 empty_patch trajectories: ~8 consecutive
# non-write steps is the inflection point where the model has either
# committed silently or stalled. Below 8: normal exploration window.
# Above 8: hesitation-suppression bias starts to apply.
# v1.11 Phase B NOTE: this threshold is RETIRED as a bias source — Phase A
# calibration (project_v111_phaseA_calibration) proved no_write is
# non-selective (read-heavy SUCCESS trajectories hit the same depths as
# stalls; precision <=31% at every threshold). consecutive_no_write_steps is
# still computed for the adaptive_state observability event, but it no longer
# biases any intervention. Kept as a constant for back-compat / future mining.
_NO_WRITE_BIAS_THRESHOLD = 8

# v1.11 Phase B — score_trend → soft_anchor. The CONSUMER (loop.py band-response
# collapse promotion) was REVERTED after Phase D: net-negative at n=75 (premature-
# commitment tier demotion on xarray-3305 + pylint-4661, 0 gains). This bias is
# retained as OBSERVABILITY ONLY — it still drives modulation_soft_anchor in the
# adaptive_state event so a future v1.11.1 redesign can see where a (more
# specific) stall signal would fire, but nothing in dispatch acts on it.
# Phase A showed empty_patch trajectories separate from patched on convergence
# VELOCITY, not raw inactivity: by step >= _COLLAPSE_MIN_STEP the empties sit
# at/below LOW with a flat/falling trend while patched have turned positive and
# climbed above LOW. When that confirmed-collapse signature holds, raise the
# score<LOW band-response intensity toward a soft_anchor commitment nudge;
# when the trajectory is self-recovering (trend > 0), suppress pressure.
# _COLLAPSE_MIN_STEP = 6: Phase A shows the conv<LOW gate becomes SELECTIVE at
# step 6 (patched median conv climbs to 0.131 > LOW; empties stay ~0.0). Step 7
# was too late — the Phase C smoke showed seaborn-3069 terminates at step 6, so
# a step-7 gate never fires on short-trajectory empties. At step 6 the conv<LOW
# gate still excludes patched (they have crossed LOW), preserving selectivity.
_COLLAPSE_MIN_STEP = 6
_COLLAPSE_CONV_CEILING = 0.10        # mirrors loop._CONVERGENCE_LOW_THRESHOLD
_SOFT_ANCHOR_COLLAPSE_BIAS = 0.5     # → modulation 1.25 (one slew step from 1.0)
_SOFT_ANCHOR_RECOVERY_BIAS = -0.3    # back off while the model converges itself


def consecutive_no_write_steps(history: Sequence[dict[str, Any]]) -> int:
    """Count of trailing non-write tool calls in the history.

    Resets to 0 the moment a write_file/edit_file is observed. Targets
    the `empty_patch` / `no_tool_call_emitted` hesitation class — the
    longer this runs, the more the trajectory is stalling without
    committing. Pure function over history.
    """
    n = 0
    for entry in reversed(list(history)):
        if entry.get("name") in _WRITE_TOOLS:
            break
        n += 1
    return n


def score_trajectory_trend(
    score_log: Sequence[float],
    window: int = _SCORE_TREND_WINDOW,
) -> float:
    """Slope sign over the last `window` convergence scores, in [-1, +1].

    Returns:
      +1.0  if scores are monotonically increasing (model is converging)
      -1.0  if scores are monotonically decreasing (drift downward — early warning)
       0.0  otherwise (mixed / flat / insufficient data)

    Uses sign of last-minus-first within the window; resilient to noise
    versus a least-squares fit because we only care about direction.
    Pure function. `score_log` is OWNED by loop.py per the agents.sdd
    composition boundary; convergence.py never mutates it.
    """
    window_vals = list(score_log)[-window:]
    if len(window_vals) < 2:
        return 0.0
    delta = window_vals[-1] - window_vals[0]
    eps = 1e-9
    if delta > eps:
        return 1.0
    if delta < -eps:
        return -1.0
    return 0.0


# ============================================================================
# Phase 4 (D) — trajectory-shape predicate (selective early_bail suppression)
# ============================================================================
#
# Background: the n=75 forge-hybrid `--no-early-bail` ablation showed
# early_bail damages SOME instances (3 wrong_target regressions) while
# protecting OTHERS. The trajectory-shape predicate detects "deep localized
# reading with stable convergence" and suppresses early_bail on those
# specific shapes — capturing the +8 fix-shape wins without re-introducing
# the 3 wrong_target damages.
#
# All four signals are pure functions of the bounded tool_history +
# score_log; the predicate composes them via locked thresholds.
W = 4  # window size for trajectory-shape signals


@dataclass(frozen=True)
class ShapeSignals:
    grep_vs_read_ratio: float
    sustained_low_trend: int
    breadth_saturation: float
    post_bail_call_rate: float


def trajectory_shape_signals(
    history: Sequence[dict[str, Any]],
    score_log: Sequence[float],
    step: int,
    *,
    early_bail_step: int | None = None,
) -> ShapeSignals:
    """Compute the 4 trajectory-shape signals used by the Phase 4 (D)
    selective early_bail suppression predicate.

    Window: last W=4 tool-call entries (or all if fewer exist).

    Signals (locked spec — DO NOT vary the formulas):
    - grep_vs_read_ratio = sum_grep / (sum_read + 1) over W
    - sustained_low_trend = max K with non-increasing convergence_score over K
    - breadth_saturation = unique_files / total_calls in W
    - post_bail_call_rate = tool_calls_in_W_post_bail / W (0.0 if no bail)
    """
    window = list(history)[-W:]

    # grep_vs_read_ratio over the window
    sum_grep = sum(1 for h in window if h.get("name") in _GREP_TOOLS)
    sum_read = sum(1 for h in window if h.get("name") in _READ_TOOLS)
    grep_vs_read_ratio = sum_grep / (sum_read + 1)

    # sustained_low_trend: walk score_log backward counting consecutive
    # non-increasing deltas (score_log[t] - score_log[t-1] <= 0).
    scores = list(score_log)
    if len(scores) < 2:
        sustained_low_trend = 0
    else:
        k = 0
        for i in range(len(scores) - 1, 0, -1):
            if scores[i] - scores[i - 1] <= 0:
                k += 1
            else:
                break
        sustained_low_trend = k

    # breadth_saturation: unique_files / total_calls in W
    total_calls = len(window)
    if total_calls == 0:
        breadth_saturation = 0.0
    else:
        unique_files = len({h.get("path") for h in window})
        breadth_saturation = unique_files / total_calls

    # post_bail_call_rate: fraction of W-window entries with step >= early_bail_step
    if early_bail_step is None:
        post_bail_call_rate = 0.0
    else:
        post_bail_count = sum(
            1 for h in window
            if h.get("step") is not None and h["step"] >= early_bail_step
        )
        post_bail_call_rate = post_bail_count / W

    return ShapeSignals(
        grep_vs_read_ratio=grep_vs_read_ratio,
        sustained_low_trend=sustained_low_trend,
        breadth_saturation=breadth_saturation,
        post_bail_call_rate=post_bail_call_rate,
    )


# Initial predicate (Phase 4 D hypothesis; testable, not retrofitted):
# Suppress early_bail when the model is in deep localized reading with
# stable (non-decreasing) convergence. The n=75 forge-hybrid plan locks
# these thresholds; refine via re-bench, not by tuning the predicate.
_SHAPE_SUPPRESS_SUSTAINED_LOW_TREND_MIN = 3
_SHAPE_SUPPRESS_GREP_VS_READ_RATIO_MAX = 0.5
_SHAPE_SUPPRESS_BREADTH_SATURATION_MAX = 0.6


def should_suppress_for_trajectory_shape(signals: ShapeSignals) -> bool:
    """Phase 4 (D) suppression predicate: 'deep localized reading with
    stable convergence' — model is converging on a real target,
    premature commit pressure hurts. Returns True to suppress.
    """
    return (
        signals.sustained_low_trend >= _SHAPE_SUPPRESS_SUSTAINED_LOW_TREND_MIN
        and signals.grep_vs_read_ratio < _SHAPE_SUPPRESS_GREP_VS_READ_RATIO_MAX
        and signals.breadth_saturation < _SHAPE_SUPPRESS_BREADTH_SATURATION_MAX
    )


@dataclass(frozen=True)
class AdaptiveState:
    """Within-run trajectory state — computed once per loop step.

    Per agents.sdd: pure value type, no mutation. Frozen so callers
    can't accidentally update fields and expect persistence (the deque
    in loop.py is the only mutable state in this subsystem).
    """
    step: int
    consecutive_no_write: int | None
    score_trend: float | None  # in {-1.0, 0.0, +1.0} or None if ablated
    score_log_len: int
    # v1.11 Phase B — current convergence score (= score_log[-1]). Needed by
    # the score_trend → soft_anchor gate to distinguish "stuck below LOW" from
    # "climbing above LOW". Default None keeps pre-Phase-B constructors valid.
    convergence_score: float | None = None


def compute_within_run_state(
    score_log: Sequence[float],
    tool_history: Sequence[dict[str, Any]],
    step: int,
    *,
    no_write_enabled: bool = True,
    score_trend_enabled: bool = True,
) -> AdaptiveState:
    """Compose the within-run state from owned inputs.

    `score_log` and `tool_history` are both owned by loop.py; this
    function reads only. Per-signal `*_enabled` flags allow ablation
    (driven by LUXE_ADAPTIVE_* env vars in loop.py).
    """
    return AdaptiveState(
        step=step,
        consecutive_no_write=(
            consecutive_no_write_steps(tool_history) if no_write_enabled else None
        ),
        score_trend=(
            score_trajectory_trend(score_log) if score_trend_enabled else None
        ),
        score_log_len=len(score_log),
        convergence_score=(score_log[-1] if score_log else None),
    )


def compute_intervention_bias(state: AdaptiveState) -> dict[str, float]:
    """Per-intervention bias deltas in [-1.0, +1.0].

    Negative bias = suppress this intervention slightly (the model is
    already trending toward the desired behavior on its own).
    Positive bias = encourage earlier/stronger firing.

    Bias-not-lock invariant: the returned deltas are advisory. The
    caller (loop.py, future phase) must clamp the final modulation to
    [_INTENSITY_MIN, _INTENSITY_MAX] AND apply slew-rate limiting AND
    refuse to let the result reach exactly 0.0 or _INTENSITY_MAX (which
    would functionally gate the intervention).
    """
    out: dict[str, float] = {}
    # v1.11 Phase B — no_write → write_pressure/early_bail bias RETIRED.
    # Phase A proved the signal is non-selective (precision <=31%); keeping it
    # active would perturb ~40% of healthy read-heavy preserves at threshold 8.
    # Pinned at 0.0 so the keys still exist for the modulation pipeline but
    # write_pressure/early_bail stay at neutral modulation (1.0). This makes
    # soft_anchor the single moving lever this cycle (one-lever discipline).
    out["write_pressure"] = 0.0
    out["early_bail"] = 0.0
    # score_trajectory_trend → soft_anchor (the single live lever).
    if state.score_trend is not None:
        if state.score_trend > 0:
            # Model converging on its own — suppress commitment pressure.
            # Ungated: backing off is always safe (preserves self-recovering
            # trajectories such as matplotlib-14623 / sphinx-10323 successes).
            out["soft_anchor"] = _SOFT_ANCHOR_RECOVERY_BIAS
        elif (state.convergence_score is not None
                and state.step >= _COLLAPSE_MIN_STEP
                and state.convergence_score < _COLLAPSE_CONV_CEILING):
            # Confirmed monotonic collapse in the score<LOW band (flat/falling
            # trend, stuck below LOW, late enough that the signal is reliable
            # per Phase A). Raise the band-response intensity toward a
            # soft_anchor commitment nudge.
            out["soft_anchor"] = _SOFT_ANCHOR_COLLAPSE_BIAS
        else:
            out["soft_anchor"] = 0.0
    return out


def apply_slew_rate(
    prev_modulation: float,
    target_modulation: float,
    max_delta: float = _DEFAULT_MAX_DELTA,
) -> float:
    """Bounded transition from prev → target modulation.

    Prevents oscillation by capping per-step intensity change. Returned
    value is always clamped to [_INTENSITY_MIN, _INTENSITY_MAX]. Pure.
    """
    if max_delta < 0:
        max_delta = 0.0
    delta = target_modulation - prev_modulation
    if delta > max_delta:
        delta = max_delta
    elif delta < -max_delta:
        delta = -max_delta
    out = prev_modulation + delta
    return max(_INTENSITY_MIN, min(_INTENSITY_MAX, out))


def bias_to_modulation(
    bias: float,
    *,
    neutral: float = _INTENSITY_NEUTRAL,
) -> float:
    """Convert a bias delta in [-1, +1] to a modulation factor in
    [_INTENSITY_MIN, _INTENSITY_MAX], centered on `neutral` (default 1.0).

    Bias-not-lock: the output is clamped strictly INSIDE [_INTENSITY_MIN,
    _INTENSITY_MAX] using a small epsilon, so no signal combination can
    produce exactly 0.0 (would gate) or _INTENSITY_MAX (would saturate).
    """
    eps = 1e-3
    raw = neutral + bias * (_INTENSITY_MAX - neutral) if bias >= 0 \
        else neutral + bias * (neutral - _INTENSITY_MIN)
    return max(_INTENSITY_MIN + eps, min(_INTENSITY_MAX - eps, raw))
