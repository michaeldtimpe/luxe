"""`LoopState` — the mutable per-run state of `run_agent`, in one record.

`run_agent` used to carry ~45 bare locals of loop state interleaved with its
configuration. They moved here VERBATIM (same names, same initial values,
same comments) so the loop body reads `st.<name>` and the state a guard
consumes is visible in one place. This is a pure move: nothing here decides
anything, and `loop.py` still owns every mutation.

What stays OUT of this record, deliberately:
- configuration read once from `RunFlags` (the `*_enabled` aliases that the
  body never reassigns, `log_calls`, the compactor) — those are locals in
  `run_agent`, read-only after the preamble;
- per-step scratch values (`pressure`, `convergence_score`, the guard
  decisions, `tool_calls`, ...) — they live and die inside one iteration;
- `result` (the `AgentResult` being built) and `resp` (the latest response),
  which the loop threads explicitly.

The four intervention gates that a SpecDD `expects_zero_calls` spec switches
off at run start (`write_pressure_enabled`, `early_bail_enabled`,
`action_density_gate_enabled`, `convergence_gate_enabled`) ARE state: their
effective value differs from the flag.

agents.sdd "Strict counter discipline": `writes_seen`, the compaction
counters and every `*_fired` flag are explicit state owned by `run_agent`,
NEVER recomputed by scanning `messages`. Keeping them as named fields here is
that rule, not a relaxation of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from luxe.agents.convergence import _INTENSITY_NEUTRAL

#: Bound on `LoopState.tool_history` (the convergence score's window).
TOOL_HISTORY_MAX = 20


def _neutral_modulation() -> dict[str, float]:
    return {
        "write_pressure": _INTENSITY_NEUTRAL,
        "early_bail": _INTENSITY_NEUTRAL,
        "soft_anchor": _INTENSITY_NEUTRAL,
    }


@dataclass
class LoopState:
    """Mutable state of one `run_agent` invocation. See module docstring."""

    # The conversation sent to the model each step (system + task first).
    messages: list[dict[str, Any]]

    # Dedup: every (name, args) key dispatched this run, and how many steps
    # in a row contained a repeat.
    seen_calls: set[str] = field(default_factory=set)
    consecutive_repeat_steps: int = 0
    # Next cumulative-completion-token mark for the [token-progress] line
    # (0 = disabled).
    next_token_log_threshold: int = 0

    # --- intervention gates (effective values; see module docstring) -------
    write_pressure_enabled: bool = False
    write_pressure_fired: bool = False
    early_bail_enabled: bool = False
    early_bail_fired: bool = False
    early_bail_step: int | None = None  # v1.9: needed by post-bail rescue gate
    # v1.9 — LUXE_ACTION_DENSITY_GATE (staged escalation second-stage rescue
    # after early_bail stalls). See guardrails' _ACTION_DENSITY_GATE_*.
    action_density_gate_enabled: bool = False
    action_density_gate_fired: bool = False
    # v1.10 — conditional intervention stacking via convergence score.
    # When enabled:
    #   - early_bail SUPPRESSED if score < _CONVERGENCE_LOW_THRESHOLD
    #     (diffuse-recon; commitment pressure hurts exploratory recovery)
    #   - early_bail MESSAGE swaps to commit_imperative when score >= HIGH
    #     and the configured mode is "soft_anchor" (the dynamic variant)
    #   - action_density_gate SUPPRESSED if score >= _CONVERGENCE_HIGH
    #     (model has converged on its own; rescue would interrupt)
    # Off by default; adapter wires it on for SWE-bench. Falls back to
    # v1.9 semantics (no convergence-based gating) when disabled.
    convergence_gate_enabled: bool = False

    # --- turn retries -------------------------------------------------------
    truncated_turn_retries_used: int = 0
    empty_turn_retries_used: int = 0

    # --- context calibration / clamp ---------------------------------------
    # Server-truth context calibration (2026-08-11). `estimate_tokens` is
    # chars//4 and reads ~1.9x low on code + JSON tool payloads, so every
    # compaction threshold fired at roughly twice the context it named. Each
    # response's `usage.prompt_tokens` corrects the next step's reading.
    # 1.0 = uncalibrated, which is both the step-1 state and the ablation.
    ctx_calibration: float = 1.0
    # The size of the prompt the live ratio was measured on; 0 = never
    # measured, which `damped_calibration` returns unchanged. Read solely by
    # the opt-in LUXE_CTX_CAL_DAMP.
    est_at_calibration: int = 0
    # Tool results bounded by LUXE_TOOL_RESULT_CLAMP this run.
    tool_results_clamped: int = 0

    # --- TieredCompact telemetry counters (forge-hybrid Phase 2 (A)) -------
    compaction_tool_results_dropped_total: int = 0
    #: Phases that fired and changed nothing (2026-08-24, telemetry only).
    compaction_ineffective_fires: int = 0
    compaction_total_tokens_dropped: int = 0
    compaction_max_phase_this_run: int = 0
    compaction_phase_at_first_write: int | None = None

    # --- v1.11 adaptive policy ---------------------------------------------
    # Modulation state per intervention kind; starts neutral (1.0 = no change).
    # Updated each step (slew-rate-limited) when adaptive_policy_enabled.
    # v1.11 status: ALL THREE modulations are computed + emitted for
    # observability but NONE acts on dispatch. The Phase B soft_anchor collapse
    # promotion was reverted (net-negative at n=75 — premature-commitment tier
    # demotion). write_pressure/early_bail bias was retired in Phase A
    # (no_write non-selective). soft_anchor bias is still computed (shows where a
    # future, more-specific stall signal would fire) but no consumer remains.
    intervention_modulation: dict[str, float] = field(
        default_factory=_neutral_modulation)
    # Bounded per-step score log; owned by loop.py per the agents.sdd
    # composition boundary (convergence.py is the sole consumer, never
    # mutates it).
    score_log: list[float] = field(default_factory=list)

    # --- early_bail band response (v1.10.4) --------------------------------
    suppression_count_in_trajectory: int = 0
    breadth_probe_fire_count: int = 0

    # v1.9 — convergence proxy. Track read_file call signatures so the gate
    # can suppress itself when the model has revisited the same file (strong
    # trajectories rerun reads ~3× more often than empties per the v18
    # distribution; that's a "found my target" signal).
    read_keys_seen: set[str] = field(default_factory=set)
    same_file_read_twice_step: int | None = None

    # v1.9 — habituation telemetry. Records the most-recent intervention fire
    # so the next step's action_density_sample can report whether the
    # intervention shifted behavior (tool call vs another prose-only turn).
    last_intervention_step: int | None = None
    last_intervention_kind: str | None = None
    # v1.10.1 — habituation clean-exit predicate state. Set tracks DISTINCT
    # intervention kinds fired this run (not count of fires). When ≥3
    # distinct kinds have fired AND first_write_step_after_intervention is
    # still None AND step ≥ _HABITUATION_EXIT_MIN_STEP, exit cleanly instead
    # of burning the remaining max_steps budget. Reads from existing
    # post-intervention telemetry; no new instrumentation required.
    intervention_kinds_fired: set[str] = field(default_factory=set)

    # v1.10 — convergence-score telemetry. tool_history is a bounded list of
    # (name, path) entries for the convergence score (see
    # luxe.agents.convergence), capped at TOOL_HISTORY_MAX. post-intervention
    # behavior signals capture whether the model engaged after a fire
    # (lag-to-write + sustained-write signals).
    tool_history: list[dict[str, Any]] = field(default_factory=list)
    first_write_step_after_intervention: int | None = None
    post_intervention_consecutive_writes: int = 0
    post_intervention_write_burst_max: int = 0
    prev_completion_tokens: int = 0
    prev_tool_calls_total_at_sample: int = 0  # v1.9 — for next_action_was_tool_call
    writes_seen: int = 0
    post_write_idle_tools: int = 0

    # SpecDD Lever 1 mid-loop state (v1.7). actual_tool_calls accumulates
    # (name, args) for every dispatched call so the spec validator sees the
    # same shape the BFCL adapter does. spec_violations_reprompted tracks
    # which requirement ids have already triggered a reprompt so each fires
    # at most once.
    actual_tool_calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list)
    spec_violations_reprompted: set[str] = field(default_factory=set)
