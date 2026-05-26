"""Guardrail interface for loop.py intervention extraction.

Forge-hybrid Phase 1 (C) deliverable. Extracts the in-line intervention surface
in loop.py (lines 757-987 + 1028-1095 + 1206-1287 + 1486-1515) into isolated,
testable Guard classes. Each guard exposes:

    check(...) -> Optional[Decision]
        Pure decision function over snapshot state. Returns a Decision if the
        guard should fire on this step, else None. No side effects.

    nudge_type: str
        Stable identifier used for the `_luxe_nudge_type` message marker and
        for observability event names.

The loop.py caller is responsible for state mutation (setting *_fired flags,
appending messages, emitting events) so this refactor preserves the existing
behavior surface while extracting the conditional logic.

Markers: when a guard fires, the loop tags the injected message dict with
`_luxe_nudge=True` and `_luxe_nudge_type=<guard.nudge_type>`. These keys ride
through backend.chat to oMLX (which ignores unknown message fields, matching
the existing `_luxe_repair` precedent) and are read by:
- Axis A TieredCompact: Phase-1 drop-nudges identifies which messages are
  decorative interventions vs load-bearing context.
- agents.sdd reflect rule: `_luxe_repair` is excluded from later verify
  contexts; the same filter pattern is available for `_luxe_nudge` if needed.

See `docs/luxe-markers-audit.md` for the full marker classification.

Two interface shapes coexist for guard outputs:

1. `Decision` — the standard shape for "fire a nudge" guards (WritePressureGuard,
   ProseBurstGuard, ActionDensityGateGuard). Carries the message body + per-fire
   metadata for the *_fired event payload. The loop appends the message and emits
   the event.
2. `should_exit(...) -> Optional[dict[str, Any]]` — sibling method for "exit the
   loop, no message" guards (HabituationExitGuard, PostWriteIdleExitGuard,
   ConsecutiveRepeatGuard). Returns the event-payload dict on fire, or None.
   The loop interprets a non-None return as "exit" and emits the corresponding
   event. Shoehorning a no-message decision into `Decision` (with `message=""`)
   adds an awkward sentinel; the sibling method keeps the contract clean.

EarlyBailGuard has a richer shape (`EarlyBailOutcome`) because the single
predicate evaluation may produce up to two events + a conditional message + state
mutations — too much for a flat Decision. The loop's role remains the same:
apply state mutations + emit events according to the outcome.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Decision:
    """A guard's decision to fire. Returned by `check()`; consumed by the loop.

    Attributes:
        message: The nudge body to inject as a user-role message.
        metadata: Per-guard observability payload, attached to the `*_fired`
            event so post-hoc analysis can see what gated the fire.
    """

    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


# Write-pressure guard constants — promoted from loop.py module-level scope so
# the guard owns its own thresholds. Values unchanged from loop.py v1.4.1.

_WRITE_PRESSURE_MIN_TOOLS = 10
_WRITE_PRESSURE_MIN_TOKENS = 4000
_WRITE_PRESSURE_MAX_TOOLS_BEFORE_FIRE = 15
_WRITE_PRESSURE_MIN_STEP = 5

_WRITE_PRESSURE_MESSAGE = (
    "Mid-loop notice: you've issued multiple reads without writing or "
    "editing any files. This task's deliverable is a concrete diff — "
    "re-reading existing material cannot produce one. Stop reading and "
    "call `write_file` or `edit_file` now with the deliverable based on "
    "what you've already learned. If specific details are missing, write "
    "a first draft that captures the structure, then refine."
)


class WritePressureGuard:
    """v1.4.1 write_pressure intervention.

    Fires once per run when the agent has done substantial reading without
    writing AND has accumulated either prose tokens (the qwen3.6-35b prose-mode
    trap) or many tool calls (the qwen3-coder tool-heavy trap). The dual
    threshold handles models with different output distributions; see the
    docstring on `_WRITE_PRESSURE_MIN_TOOLS` in loop.py for the empirical basis.

    Adaptive policy: when LUXE_ADAPTIVE_POLICY=1, the effective min_step is
    modulated by intervention_modulation["write_pressure"]. mod=1.0 (neutral,
    default) preserves v1.10.5 behavior exactly. mod>1.0 fires earlier (bias
    toward encouragement); mod<1.0 fires later (bias toward suppression).
    Bias-not-lock invariant holds: effective_min_step stays strictly inside
    the bounded range because the eps-clamped modulation can't reach the
    endpoints. See agents.sdd Stage 3 invariants.
    """

    nudge_type = "write_pressure"

    @staticmethod
    def check(
        *,
        write_pressure_enabled: bool,
        write_pressure_fired: bool,
        writes_seen: int,
        step: int,
        tool_calls_total: int,
        completion_tokens: int,
        adaptive_policy_enabled: bool,
        intervention_modulation_write_pressure: float,
    ) -> Optional[Decision]:
        if not write_pressure_enabled:
            return None
        if write_pressure_fired:
            return None
        if writes_seen != 0:
            return None
        if adaptive_policy_enabled:
            effective_min_step = max(
                2,
                int(round(_WRITE_PRESSURE_MIN_STEP * (2.0 - intervention_modulation_write_pressure))),
            )
        else:
            effective_min_step = _WRITE_PRESSURE_MIN_STEP
        if step < effective_min_step:
            return None
        if tool_calls_total < _WRITE_PRESSURE_MIN_TOOLS:
            return None
        if (completion_tokens < _WRITE_PRESSURE_MIN_TOKENS
                and tool_calls_total < _WRITE_PRESSURE_MAX_TOOLS_BEFORE_FIRE):
            return None
        return Decision(
            message=_WRITE_PRESSURE_MESSAGE,
            metadata={
                "tool_calls_total": tool_calls_total,
                "completion_tokens": completion_tokens,
            },
        )


# Prose-burst guard constants. Duplicated from loop.py v1.8 Track 1.

_PROSE_BURST_MAX_STEP = 4
_PROSE_BURST_MIN_DELTA = 1500

_PROSE_BURST_MESSAGE = (
    "Mid-loop notice: your previous response generated significant text "
    "without invoking any tool. The deliverable for this task is a "
    "concrete action, not a written explanation. Your next response must "
    "either (a) emit a tool call to gather information you need, or "
    "(b) emit a write/edit tool call to commit your solution. Reasoning "
    "in text without calling a tool is not progress."
)


class ProseBurstGuard:
    """v1.8 Track 1 prose-burst intervention.

    Fires at most once per run when a step <= MAX_STEP has produced no tool
    calls, no writes, and a per-step completion-token burst above MIN_DELTA.
    The composite invariant catches the short-trace bailer class (B.1 audit
    archetype) where the model exited at step <=3 with 8000+ completion
    tokens — prose burst without action.

    The anti-oscillation "clean exit on second burst" branch STAYS in
    loop.py — it depends on the same predicate but does not append a
    nudge; instead it breaks the loop with aborted=False. The loop owns
    that branch and computes `prose_burst_now` independently for it.
    """

    nudge_type = "prose_burst"

    @staticmethod
    def check(
        *,
        prose_burst_enabled: bool,
        prose_burst_fired: bool,
        step: int,
        tool_calls_total: int,
        writes_seen: int,
        completion_delta_last_step: int,
    ) -> Optional[Decision]:
        if not prose_burst_enabled:
            return None
        if prose_burst_fired:
            return None
        prose_burst_now = (
            step <= _PROSE_BURST_MAX_STEP
            and tool_calls_total == 0
            and writes_seen == 0
            and completion_delta_last_step >= _PROSE_BURST_MIN_DELTA
        )
        if not prose_burst_now:
            return None
        return Decision(
            message=_PROSE_BURST_MESSAGE,
            metadata={
                "completion_delta": completion_delta_last_step,
            },
        )


# Action-density-gate guard constants. Duplicated from loop.py v1.9.

_ACTION_DENSITY_GATE_MIN_STEP = 6
_ACTION_DENSITY_GATE_MIN_TOKENS = 1500
_ACTION_DENSITY_GATE_MAX_TOOLS = 10
_ACTION_DENSITY_GATE_MIN_TURNS_AFTER_BAIL = 2

_ACTION_DENSITY_GATE_MESSAGE = (
    "Mid-loop notice: you have generated significant reasoning but have "
    "produced very few tool calls and no edits. This pattern — high "
    "token output, low action — usually means analysis without "
    "commitment. Choose the highest-probability bug location based on "
    "what you've already established, and emit a concrete patch now "
    "using `write_file` or `edit_file`. Commit to your best candidate "
    "rather than continuing to deliberate."
)


class ActionDensityGateGuard:
    """v1.9 action_density_gate — staged-escalation second-stage rescue.

    Fires at most once per run in one of two modes:
      - standalone: early_bail never fired; gate stands alone
      - post_bail_rescue: early_bail fired >= MIN_TURNS_AFTER_BAIL turns ago
        AND no writes since
    Convergence proxy (same_file_read_twice on/before this step) suppresses
    the gate when convergence_gate_enabled is off (v1.9 fallback). When
    convergence_gate_enabled is on, the v1.10 high-score branch suppresses
    instead — that branch lives in loop.py because it emits a *different*
    event (`action_density_gate_suppressed_converged`) and does NOT append
    a message. This guard only handles the actual nudge case.
    """

    nudge_type = "action_density_gate"

    @staticmethod
    def check(
        *,
        action_density_gate_enabled: bool,
        action_density_gate_fired: bool,
        writes_seen: int,
        step: int,
        completion_tokens: int,
        tool_calls_total: int,
        v110_suppress: bool,
        v19_suppress: bool,
        early_bail_step: Optional[int],
    ) -> Optional[Decision]:
        if not action_density_gate_enabled:
            return None
        if action_density_gate_fired:
            return None
        if writes_seen != 0:
            return None
        if step < _ACTION_DENSITY_GATE_MIN_STEP:
            return None
        if completion_tokens < _ACTION_DENSITY_GATE_MIN_TOKENS:
            return None
        if tool_calls_total > _ACTION_DENSITY_GATE_MAX_TOOLS:
            return None
        if v110_suppress or v19_suppress:
            return None
        if early_bail_step is None:
            fire_mode = "standalone"
            turns_since_bail: Optional[int] = None
        elif step - early_bail_step >= _ACTION_DENSITY_GATE_MIN_TURNS_AFTER_BAIL:
            fire_mode = "post_bail_rescue"
            turns_since_bail = step - early_bail_step
        else:
            # bail grace period — hold gate this step.
            return None
        return Decision(
            message=_ACTION_DENSITY_GATE_MESSAGE,
            metadata={
                "fire_mode": fire_mode,
                "turns_since_bail": turns_since_bail,
            },
        )


# Habituation-exit guard constant. Duplicated from loop.py v1.10.1.

_HABITUATION_EXIT_MIN_STEP = 20
_HABITUATION_EXIT_MIN_KINDS = 3


class HabituationExitGuard:
    """v1.10.1 habituation clean-exit.

    NOT a nudge guard: when the predicate fires, the loop should break
    cleanly (aborted=False) — no message is appended. Exposed via
    `should_exit()` returning the event payload dict (or None) instead of
    the `Decision` interface, which is a poor fit for no-message exits.

    Predicate: >= MIN_KINDS distinct interventions fired AND no
    post-intervention writes yet AND step >= MIN_STEP. The trajectory is
    intervention-resistant; burning the remaining max_steps yields no
    further information.
    """

    @staticmethod
    def should_exit(
        *,
        intervention_kinds_fired: set[str],
        first_write_step_after_intervention: Optional[int],
        step: int,
        last_intervention_step: Optional[int],
        tool_calls_total: int,
        completion_tokens: int,
    ) -> Optional[dict[str, Any]]:
        if len(intervention_kinds_fired) < _HABITUATION_EXIT_MIN_KINDS:
            return None
        if first_write_step_after_intervention is not None:
            return None
        if step < _HABITUATION_EXIT_MIN_STEP:
            return None
        return {
            "interventions_fired": sorted(intervention_kinds_fired),
            "since_last_intervention": (
                step - last_intervention_step
                if last_intervention_step is not None else None
            ),
            "tool_calls_total": tool_calls_total,
            "completion_tokens": completion_tokens,
        }


# Post-write-idle-exit guard constant. Duplicated from loop.py.
_POST_WRITE_IDLE_MAX = 3


class PostWriteIdleExitGuard:
    """Post-write drift detector.

    NOT a nudge guard: when fired, loop breaks cleanly (aborted=False) —
    no message appended. Same shape as HabituationExitGuard: exposed via
    `should_exit()` returning the event payload dict or None.

    Once at least one write has succeeded, any run of non-write tool calls
    returning zero bytes (or hitting the dedup short-circuit) signals
    "diff already produced, model is spinning on verification without new
    information". Exit cleanly at this many back-to-back idle calls so the
    harness still commits/pushes the work without burning the full
    max_steps budget.
    """

    @staticmethod
    def should_exit(
        *,
        post_write_idle_tools: int,
        writes_seen: int,
    ) -> Optional[dict[str, Any]]:
        if post_write_idle_tools < _POST_WRITE_IDLE_MAX:
            return None
        return {
            "idle_tools": post_write_idle_tools,
            "writes_seen": writes_seen,
        }


# Consecutive-repeat guard constant. Duplicated from loop.py.
_MAX_CONSECUTIVE_REPEAT_STEPS = 2


class ConsecutiveRepeatGuard:
    """Consecutive-repeat (stuck-loop) abort guard.

    NOT a nudge guard: when fired, loop ABORTS (aborted=True) with
    abort_reason="stuck_loop"-shape message. The loop owns the
    final_text/aborted/abort_reason assignment plus the event emission.

    Exposed via `should_abort()` returning a dict with the abort_reason +
    event payload, or None. Same no-message shape as HabituationExitGuard
    but with abort semantics rather than clean-exit.
    """

    @staticmethod
    def should_abort(
        *,
        consecutive_repeat_steps: int,
    ) -> Optional[dict[str, Any]]:
        if consecutive_repeat_steps < _MAX_CONSECUTIVE_REPEAT_STEPS:
            return None
        return {
            "consecutive_repeat_steps": consecutive_repeat_steps,
            "abort_reason": (
                f"Stuck in loop — repeated same tool calls "
                f"{consecutive_repeat_steps} consecutive turns"
            ),
        }


# Early-bail guard constants + messages. Duplicated from loop.py v1.7+.

_EARLY_BAIL_MIN_STEP = 4
_EARLY_BAIL_MIN_READS = 4

_EARLY_BAIL_MESSAGE = (
    "Mid-loop notice: you have explored the repository but haven't proposed "
    "any edits yet. Choose the single file most likely to need modification "
    "and produce a concrete diff now using `write_file` or `edit_file`. If "
    "after this exploration you believe the existing code is correct as-is, "
    "say so explicitly with the file path you investigated and the reason — "
    "do not continue reading."
)

_EARLY_BAIL_MESSAGE_NO_ABSTAIN = (
    "Mid-loop notice: you have explored the repository but haven't proposed "
    "any edits yet. The fix exists in this repository. Choose the single "
    "file most likely to need modification and produce a concrete diff now "
    "using `write_file` or `edit_file`. Do not continue reading; commit to "
    "an edit based on what you've already learned."
)

_EARLY_BAIL_MESSAGE_SOFT_ANCHOR = (
    "Mid-loop notice: you have explored the repository but haven't proposed "
    "any edits yet. Based on what you've already read, choose the "
    "highest-probability bug location — even if uncertain — and emit a "
    "concrete patch now using `write_file` or `edit_file`. Commit to your "
    "best candidate."
)

_EARLY_BAIL_MESSAGE_COMMIT_IMPERATIVE = (
    "Mid-loop notice: your read pattern indicates you have identified "
    "the likely target. Commit to the most promising file and attempt "
    "the smallest viable corrective edit using `write_file` or "
    "`edit_file` now."
)

_EARLY_BAIL_MESSAGE_BREADTH_PROBE = (
    "Mid-loop status: your read pattern is broad without a clear "
    "hypothesis converging. If you have a candidate bug location, "
    "focus your next reads on its function bodies and surrounding "
    "context to confirm or rule it out. If you do not yet have a "
    "candidate, continue gathering signal."
)

_EARLY_BAIL_MESSAGE_MODES: dict[str, str] = {
    "default": _EARLY_BAIL_MESSAGE,
    "no_abstain": _EARLY_BAIL_MESSAGE_NO_ABSTAIN,
    "soft_anchor": _EARLY_BAIL_MESSAGE_SOFT_ANCHOR,
    "commit_imperative": _EARLY_BAIL_MESSAGE_COMMIT_IMPERATIVE,
    "breadth_probe": _EARLY_BAIL_MESSAGE_BREADTH_PROBE,
}

_BREADTH_PROBE_ESCALATION_COUNT = 3

_CONVERGENCE_LOW_THRESHOLD = 0.10
_CONVERGENCE_HIGH_THRESHOLD = 0.40


def _v1105_synthesis_looping_signature(bm25_count: int,
                                       grep_count: int,
                                       distinct_files: int) -> bool:
    """True iff the trajectory shows the bm25-without-grep pattern AND
    has accumulated breadth (distinct_files >= 2) at suppression #1.
    This isolates sphinx-10323's synthesis-wandering pathology from
    sympy-12419's premature-loop-kill pathology (both share bm25=1+
    grep=0 but differ in distinct_files).
    Exposed as a function for unit-testability."""
    return bm25_count > 0 and grep_count == 0 and distinct_files >= 2


@dataclass(frozen=True)
class EarlyBailOutcome:
    """Structured outcome from EarlyBailGuard.evaluate().

    The early-bail predicate can produce up to two events + a conditional
    nudge in a single evaluation, more than `Decision` can carry. Fields:

    - decision: Optional[Decision]. The nudge to append (None if no message
      should be appended this step). Decision.metadata carries event-payload
      fields for the corresponding *_fired event.
    - nudge_type: Optional[str]. The `_luxe_nudge_type` marker for the
      injected message. Varies by variant (e.g. "early_bail_soft_anchor",
      "early_bail_breadth_probe", "early_bail_commit_imperative"). None if
      no message is appended.
    - last_intervention_kind: Optional[str]. The kind string the loop should
      record in last_intervention_kind / intervention_kinds_fired. None when
      no intervention is recorded (suppress_diffuse without breadth_probe).
    - sets_early_bail_fired: bool. Whether the loop should set the
      early_bail_fired flag this step.
    - suppression_count_delta: int (0 or 1). How much the loop should
      increment suppression_count_in_trajectory (always 0 except in the
      suppress_diffuse branch).
    - breadth_probe_fire_delta: int (0 or 1). Likewise for
      breadth_probe_fire_count.
    - suppress_event: Optional[(name, payload)]. Suppression-observability
      event the loop should emit IN ADDITION to any fired event (e.g.,
      `early_bail_suppressed_diffuse` paired with optional
      `early_bail_breadth_probe_fired`, or `early_bail_suppressed_commit_only`
      standalone).
    - fire_event: Optional[(name, payload)]. The fired-event the loop should
      emit (e.g., `early_bail_fired` or `early_bail_breadth_probe_fired`).
    """

    decision: Optional[Decision] = None
    nudge_type: Optional[str] = None
    last_intervention_kind: Optional[str] = None
    sets_early_bail_fired: bool = False
    suppression_count_delta: int = 0
    breadth_probe_fire_delta: int = 0
    suppress_event: Optional[tuple[str, dict[str, Any]]] = None
    fire_event: Optional[tuple[str, dict[str, Any]]] = None


class EarlyBailGuard:
    """v1.7+ early-bail intervention (the largest guard).

    Fires at step >= MIN_STEP with reads >= MIN_READS and zero writes. The
    actual variant selected depends on:
      - explicit `early_bail_message` kwarg (highest precedence; static)
      - LUXE_EARLY_BAIL_MODE env (default/no_abstain/soft_anchor/...; static
        for non-soft_anchor modes)
      - convergence-gate score band when mode is soft_anchor:
          score < LOW: suppress (with optional breadth_probe hybrid first-
                      event + escalation count fire)
          LOW <= score < HIGH: soft_anchor fires
          score >= HIGH: commit_imperative variant fires (tighter)
      - LUXE_EARLY_BAIL_COMMIT_ONLY refined-port: suppresses soft_anchor at
        mid band; only commit_imperative fires.

    The `_luxe_nudge_type` marker reflects the variant fired (e.g.,
    "early_bail_soft_anchor", "early_bail_breadth_probe") so compaction can
    differentiate.
    """

    nudge_type = "early_bail"  # base type; per-variant marker overrides

    @staticmethod
    def evaluate(
        *,
        early_bail_enabled: bool,
        early_bail_fired: bool,
        writes_seen: int,
        step: int,
        tool_calls_total: int,
        early_bail_message: Optional[str],
        early_bail_commit_only: bool,
        convergence_gate_enabled: bool,
        convergence_score: float,
        band_response: str,
        suppression_count_in_trajectory: int,
        tool_history: list[dict[str, Any]],
        recent_path_diversity: float,
    ) -> Optional[EarlyBailOutcome]:
        if not early_bail_enabled:
            return None
        if early_bail_fired:
            return None
        if writes_seen != 0:
            return None
        if step < _EARLY_BAIL_MIN_STEP:
            return None
        if (tool_calls_total - writes_seen) < _EARLY_BAIL_MIN_READS:
            return None

        # Resolution precedence: explicit kwarg > env mode > default.
        # Only soft_anchor mode is dynamic; static modes (default,
        # no_abstain, explicit kwarg) bypass the band logic.
        suppress_for_convergence = (
            convergence_gate_enabled
            and convergence_score < _CONVERGENCE_LOW_THRESHOLD
            and early_bail_message is None
            and os.environ.get("LUXE_EARLY_BAIL_MODE", "default") == "soft_anchor"
        )

        if suppress_for_convergence:
            # Suppress branch — emit `early_bail_suppressed_diffuse` every
            # step; optionally fire breadth_probe on first event /
            # escalation per the hybrid band_response.
            new_suppression_count = suppression_count_in_trajectory + 1
            bm25_count = sum(
                1 for h in tool_history
                if h.get("name") == "bm25_search"
            )
            grep_count = sum(
                1 for h in tool_history
                if h.get("name") == "grep"
            )
            distinct_files = len({
                h.get("path") for h in tool_history
                if h.get("name") in ("read_file", "edit_file", "write_file")
                and h.get("path")
            })
            narrow_reader_signal = not _v1105_synthesis_looping_signature(
                bm25_count, grep_count, distinct_files
            )
            suppress_event = (
                "early_bail_suppressed_diffuse",
                {
                    "convergence_score": convergence_score,
                    "threshold": _CONVERGENCE_LOW_THRESHOLD,
                    "tool_calls_total": tool_calls_total,
                    "recent_path_diversity": recent_path_diversity,
                    "suppression_count_so_far": new_suppression_count,
                    "band_response": band_response,
                    "narrow_reader_signal": narrow_reader_signal,
                    "bm25_count": bm25_count,
                    "grep_count": grep_count,
                    "distinct_files": distinct_files,
                },
            )
            # Hybrid breadth_probe fire on first event / Nth escalation —
            # gated off when early_bail_commit_only refined port is set.
            if band_response == "breadth_probe_hybrid" and not early_bail_commit_only:
                fire_reason: Optional[str] = None
                if new_suppression_count == 1 and narrow_reader_signal:
                    fire_reason = "first"
                elif new_suppression_count == _BREADTH_PROBE_ESCALATION_COUNT:
                    fire_reason = "escalation"
                if fire_reason is not None:
                    fire_event = (
                        "early_bail_breadth_probe_fired",
                        {
                            "convergence_score": convergence_score,
                            "suppression_count_so_far": new_suppression_count,
                            "fire_reason": fire_reason,
                            "recent_path_diversity": recent_path_diversity,
                            "narrow_reader_signal": narrow_reader_signal,
                            "bm25_count": bm25_count,
                            "grep_count": grep_count,
                            "distinct_files": distinct_files,
                        },
                    )
                    return EarlyBailOutcome(
                        decision=Decision(
                            message=_EARLY_BAIL_MESSAGE_BREADTH_PROBE,
                            metadata={},
                        ),
                        nudge_type="early_bail_breadth_probe",
                        last_intervention_kind="early_bail_breadth_probe",
                        sets_early_bail_fired=False,
                        suppression_count_delta=1,
                        breadth_probe_fire_delta=1,
                        suppress_event=suppress_event,
                        fire_event=fire_event,
                    )
            # Suppress-only (no breadth_probe fire this step).
            return EarlyBailOutcome(
                decision=None,
                nudge_type=None,
                last_intervention_kind=None,
                sets_early_bail_fired=False,
                suppression_count_delta=1,
                breadth_probe_fire_delta=0,
                suppress_event=suppress_event,
                fire_event=None,
            )

        # Non-suppress branch — pick message + variant.
        msg: Optional[str]
        msg_variant: str
        if early_bail_message is not None:
            msg = early_bail_message
            msg_variant = "kwarg"
        else:
            mode = os.environ.get("LUXE_EARLY_BAIL_MODE", "default")
            if (convergence_gate_enabled
                    and mode == "soft_anchor"
                    and convergence_score >= _CONVERGENCE_HIGH_THRESHOLD):
                msg = _EARLY_BAIL_MESSAGE_COMMIT_IMPERATIVE
                msg_variant = "commit_imperative"
            elif early_bail_commit_only and mode == "soft_anchor":
                msg = None
                msg_variant = "suppressed_commit_only"
            else:
                msg = _EARLY_BAIL_MESSAGE_MODES.get(mode, _EARLY_BAIL_MESSAGE)
                msg_variant = mode
        if msg is not None:
            fire_event = (
                "early_bail_fired",
                {
                    "tool_calls_total": tool_calls_total,
                    "completion_tokens": None,  # filled by loop (it owns the counter)
                    "reads": tool_calls_total - writes_seen,
                    "convergence_score": convergence_score,
                    "msg_variant": msg_variant,
                },
            )
            return EarlyBailOutcome(
                decision=Decision(message=msg, metadata={}),
                nudge_type=f"early_bail_{msg_variant}",
                last_intervention_kind="early_bail",
                sets_early_bail_fired=True,
                suppression_count_delta=0,
                breadth_probe_fire_delta=0,
                suppress_event=None,
                fire_event=fire_event,
            )
        # Refined-port observability: record suppression so post-hoc analysis
        # can see how often commit_only saved a mid-band soft_anchor fire.
        suppress_event = (
            "early_bail_suppressed_commit_only",
            {
                "tool_calls_total": tool_calls_total,
                "completion_tokens": None,  # filled by loop
                "reads": tool_calls_total - writes_seen,
                "convergence_score": convergence_score,
                "configured_mode": os.environ.get("LUXE_EARLY_BAIL_MODE", "default"),
            },
        )
        return EarlyBailOutcome(
            decision=None,
            nudge_type=None,
            last_intervention_kind=None,
            sets_early_bail_fired=False,
            suppression_count_delta=0,
            breadth_probe_fire_delta=0,
            suppress_event=suppress_event,
            fire_event=None,
        )
