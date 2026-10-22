"""Shared agent loop — tool dispatch, schema validation, telemetry.

Mirrors luxe's agents/base.py run_agent() pattern: chat → parse tool calls →
validate → dispatch → append results → repeat until done or budget exhausted.
"""

from __future__ import annotations

import hashlib
import json
import os as _os_for_logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from luxe.agents.convergence import (
    apply_slew_rate,
    bias_to_modulation,
    compute_convergence_score,
    compute_intervention_bias,
    compute_within_run_state,
    extract_path,
    recent_path_diversity,
)
from luxe.agents.guardrails import (
    ActionDensityGateGuard,
    ConsecutiveRepeatGuard,
    EarlyBailGuard,
    HabituationExitGuard,
    PostWriteIdleExitGuard,
    WritePressureGuard,
)

# Intervention thresholds + nudge bodies live in guardrails.py, the sole home
# (the 2026-05-26 extraction copied rather than moved them; the copies here were
# byte-identical). Imported — not redefined — so `from luxe.agents.loop import
# _X` keeps resolving for the loop test modules and stays the SAME object as
# guardrails' (pinned by tests/test_guardrails_identity.py).
from luxe.agents.guardrails import (  # noqa: F401  (re-exported for tests)
    _ACTION_DENSITY_GATE_MAX_TOOLS,
    _ACTION_DENSITY_GATE_MESSAGE,
    _ACTION_DENSITY_GATE_MIN_STEP,
    _ACTION_DENSITY_GATE_MIN_TOKENS,
    _ACTION_DENSITY_GATE_MIN_TURNS_AFTER_BAIL,
    _BREADTH_PROBE_ESCALATION_COUNT,
    _CONVERGENCE_HIGH_THRESHOLD,
    _DEDUP_MESSAGE_TEMPLATE,
    _SPEC_ZERO_CALLS_DECLINE_MESSAGE,
    dedup_message,
    _CONVERGENCE_LOW_THRESHOLD,
    _EARLY_BAIL_MESSAGE,
    _EARLY_BAIL_MESSAGE_BREADTH_PROBE,
    _EARLY_BAIL_MESSAGE_COMMIT_IMPERATIVE,
    _EARLY_BAIL_MESSAGE_MODES,
    _EARLY_BAIL_MESSAGE_NO_ABSTAIN,
    _EARLY_BAIL_MESSAGE_SOFT_ANCHOR,
    _EARLY_BAIL_MIN_READS,
    _EARLY_BAIL_MIN_STEP,
    _HABITUATION_EXIT_MIN_KINDS,
    _HABITUATION_EXIT_MIN_STEP,
    _MAX_CONSECUTIVE_REPEAT_STEPS,
    _POST_WRITE_IDLE_MAX,
    _EMPTY_TURN_MESSAGE,
    _TRUNCATED_TURN_MESSAGE,
    EmptyTurnGuard,
    TruncatedTurnGuard,
    _WRITE_PRESSURE_MAX_TOOLS_BEFORE_FIRE,
    _WRITE_PRESSURE_MESSAGE,
    _WRITE_PRESSURE_MIN_STEP,
    _WRITE_PRESSURE_MIN_TOKENS,
    _WRITE_PRESSURE_MIN_TOOLS,
    _v1105_synthesis_looping_signature,
)
from luxe.agents.flags import RunFlags
from luxe.agents.loopstate import TOOL_HISTORY_MAX, LoopState
from luxe.backend import Backend, ChatResponse, ToolCallResponse
from luxe.config import RoleConfig
from luxe.context import (
    TieredCompact,
    calibrated_ctx_limit,
    calibration_ratio,
    clamp_tool_result,
    context_pressure,
    damped_calibration,
    elide_old_tool_results,
    estimate_messages_tokens,
    estimate_tools_tokens,
    tool_result_clamp_chars,
)
from luxe.run_state import append_event
from luxe.spec import Spec
from luxe.spec_validator import validate as spec_validate
from luxe.tools.base import ToolDef, ToolCall, ToolFn, dispatch_tool, validate_args

# DEBUG-only forensics (2026-07-31): dispatch-time tool lines + per-step ctx
# pressure. In chat sessions these land in ~/.luxe/sessions/<id>/debug.log
# (chat/debuglog.py attaches a DEBUG handler to the `luxe` package logger); on
# the benchmark path no handler is configured, so they are dropped without
# touching stdout — the [token-progress] print and events.jsonl are unchanged.
import logging as _logging

logger = _logging.getLogger(__name__)


def _args_preview(arguments, cap: int = 300) -> str:
    """Compact single-line rendering of tool args for the debug log."""
    try:
        s = json.dumps(arguments, ensure_ascii=False, default=str)
    except Exception:
        s = repr(arguments)
    s = s.replace("\n", "\\n")
    return s if len(s) <= cap else s[:cap] + "…"


@dataclass
class AgentResult:
    final_text: str = ""
    steps: int = 0
    tool_calls_total: int = 0
    schema_rejects: int = 0
    aborted: bool = False
    abort_reason: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Server-reported prompt size of the LAST step (prompt_tokens above is the
    # per-step SUM). This is the true context fill: it includes tool schemas
    # and everything else the chars/4 pressure estimate misses. Additive
    # reporting field only — no loop logic reads it (compaction/early-bail
    # keep the estimate, so benchmark behavior is untouched).
    last_prompt_tokens: int = 0
    # USD billed for this whole turn — the SUM of every `backend.chat` the loop
    # made (a turn is many steps, and each one is separately metered). Stays
    # 0.0 on every local engine, which never reports a cost. Additive reporting
    # field only: no loop logic reads it, so benchmark behavior is untouched.
    cost_usd: float = 0.0
    # Prose the model emitted on ACTING steps (text alongside tool calls), in
    # order. Additive reporting field: `final_text` is untouched and no
    # benchmark grader reads this. The chat layer joins it ahead of the final
    # answer so a reply stops starting mid-thought (see loop body).
    step_texts: list[str] = field(default_factory=list)
    # SERVER-reported cache + prefill/decode accounting, summed over the steps
    # whose response carried it (oMLX `usage`, llama-server `timings`; see
    # backend._server_perf). Additive reporting fields only — no loop logic
    # reads them. `perf_prompt_tokens` is the prompt-token denominator for
    # `cached_tokens` (steps that reported a cache count); `first_step_*` is
    # the turn's opening request — the one a cross-turn cache would serve.
    cached_tokens: int = 0
    perf_prompt_tokens: int = 0
    prefill_s: float = 0.0
    prefill_tokens: int = 0
    decode_s: float = 0.0
    decode_tokens: int = 0
    first_step_prompt_tokens: int = 0
    first_step_cached_tokens: int | None = None
    wall_s: float = 0.0
    peak_context_pressure: float = 0.0
    final_context_pressure: float = 0.0  # last per-step pressure (matches token-progress)
    # The conversation as the server last saw it (post-compaction) plus the
    # final answer — what a chat turn hands the NEXT turn as `prior_messages`
    # so its prompt extends this one and the server's prefix cache serves it
    # (chat/turn.py turn carry). Additive reporting field: no loop logic and
    # no benchmark grader reads it. `carried` = this run started from one.
    messages: list[dict[str, Any]] = field(default_factory=list)
    carried: bool = False


OnToolEvent = Callable[[ToolCall], None]


def _parse_text_tool_calls(
    text: str,
    known_names: set[str],
    drops: list[str] | None = None,
) -> list[ToolCallResponse]:
    """Recover tool calls from text when model doesn't use structured output.

    A well-formed candidate whose name is outside `known_names` is dropped —
    the caller gets no call and the model gets no tool message, so nothing
    downstream records that it happened. Pass `drops` to collect those names
    (taxonomy 2026-08-04: this was an unmeasurable failure class).
    """
    calls: list[ToolCallResponse] = []

    # Qwen/Hermes: <tool_call>{"name":...,"arguments":...}</tool_call>
    for m in re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.DOTALL):
        try:
            obj = json.loads(m.group(1))
            name = obj.get("name", "")
            args = obj.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
            if name in known_names:
                calls.append(ToolCallResponse(id="", name=name, arguments=args))
                return calls  # first only
            if drops is not None and name and name not in drops:
                drops.append(name)
        except (json.JSONDecodeError, KeyError):
            continue

    # Bare JSON: {"name": "...", "arguments": {...}}
    for m in re.finditer(r'\{\s*"name"\s*:\s*"(\w+)".*?\}', text, re.DOTALL):
        try:
            # Try to parse the full match as JSON
            start = m.start()
            depth = 0
            end = start
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            obj = json.loads(text[start:end])
            name = obj.get("name", "")
            args = obj.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
            if name in known_names:
                calls.append(ToolCallResponse(id="", name=name, arguments=args))
                return calls
            if drops is not None and name and name not in drops:
                drops.append(name)
        except (json.JSONDecodeError, KeyError):
            continue

    return calls


# Tools exempt from duplicate-call detection. Reads are idempotent in name
# but post-write semantics differ — re-reading after an edit returns the
# updated content, which the model relies on to verify edits landed.
# Deduplicating reads strands the model: it tries to verify a write,
# gets "you already called this" instead of fresh content, panics,
# and retries the write — which then trips the streak-abort. Only
# write/search tools where re-running yields no new information stay
# in the dedup path.
_DEDUP_EXEMPT_TOOLS = {"read_file"}

# Tools considered "write" actions for mid-loop write-pressure detection.
# Tasks that produce a deliverable diff must hit at least one of these — a
# loop that reads many times without ever writing is the prose-mode trap
# observed on nothing-ever-happens-document-config (v1.4.0 rep 1: 17 tool
# calls, 9092 completion tokens, 0 writes; model declared "comprehensive
# picture" prematurely, hallucinated content from priors, never committed).
_WRITE_TOOLS = frozenset({"write_file", "edit_file"})

# forge-hybrid Phase 2 (A) — recovery-marker event names by tool. Emitted on
# every tool_call after compaction has fired anywhere in the run; characterizes
# whether compaction is followed by productive read/grep/edit activity.
_COMPACTION_RECOVERY_EVENT_BY_TOOL: dict[str, str] = {
    "read_file": "read_after_compact",
    "grep": "grep_after_compact",
    "write_file": "edit_after_compact",
    "edit_file": "edit_after_compact",
}

# Emit a progress line each time cumulative completion tokens crosses a
# multiple of this threshold. Useful for spotting bailout vs full-engagement
# patterns mid-run. Set to 0 to disable. Configurable via env.
try:
    _TOKEN_LOG_INTERVAL = int(_os_for_logging.environ.get("LUXE_TOKEN_LOG_INTERVAL", "5000"))
except ValueError:  # malformed degrades silently, like every other knob —
    _TOKEN_LOG_INTERVAL = 5000  # it used to crash `import luxe.agents.loop`


def _call_key(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True)}"


def _event_emitter(run_id: str | None, phase: str,
                   enabled: bool) -> Callable[..., None]:
    """The one writer of a run's events.jsonl records.

    `emit(kind, **fields)` is exactly the `if log_calls: append_event(run_id,
    kind, phase=phase, **fields)` block it replaced, so every record keeps its
    field names AND order (`phase` first, then the fields as passed) —
    `scripts/toolcall_taxonomy.py` and `agents/outcomes.py` parse them.
    `append_event` is resolved from this module at call time, so tests that
    monkeypatch `luxe.agents.loop.append_event` still intercept every record.
    """
    def emit(kind: str, **fields: Any) -> None:
        if enabled:
            append_event(run_id, kind, phase=phase, **fields)
    return emit


def _user_message(content: str, nudge_type: str | None = None) -> dict[str, Any]:
    """A user-role message the LOOP authors. With `nudge_type` it carries the
    `_luxe_nudge` markers TieredCompact identifies droppable nudges by
    (agents.sdd "Marker-based nudge identification"); without, a plain user
    message (spec reprompts, the zero-calls decline)."""
    msg: dict[str, Any] = {"role": "user", "content": content}
    if nudge_type is not None:
        msg["_luxe_nudge"] = True
        msg["_luxe_nudge_type"] = nudge_type
    return msg


def _tool_message(tc: ToolCallResponse, step: int, content: str) -> dict[str, Any]:
    """The `role: "tool"` message answering one call. A call the model sent
    without an id gets `call_<step>`."""
    return {
        "role": "tool",
        "tool_call_id": tc.id or f"call_{step}",
        "name": tc.name,
        "content": content,
    }


def _record_intervention(st: LoopState, step: int, kind: str) -> None:
    """Mark an intervention as the latest to fire (habituation telemetry and
    the habituation exit read these)."""
    st.last_intervention_step = step
    st.last_intervention_kind = kind
    st.intervention_kinds_fired.add(kind)


def _display(cb: Callable[[Any], None] | None, arg: Any, what: str) -> None:
    """Display-only callbacks (chat front-end; None on the benchmark path):
    a broken one must not kill the run."""
    if cb is None:
        return
    try:
        cb(arg)
    except Exception:
        logger.debug("%s raised", what, exc_info=True)


@dataclass(frozen=True)
class _Run:
    """One `run_agent` call's CONFIGURATION, built once before the first step
    and never reassigned: the caller's arguments, `RunFlags`, and what is
    derived from them. The step helpers below read it; everything they
    mutate is `LoopState` / `AgentResult` (agents.sdd: loop.py is the sole
    mutator)."""

    role_cfg: RoleConfig
    flags: RunFlags
    tool_defs: list[ToolDef]
    tool_fns: dict[str, ToolFn]
    tool_def_map: dict[str, ToolDef]
    known_names: set[str]
    openai_tools: list[dict[str, Any]] | None
    # Estimated tokens of the tool schemas (LUXE_CTX_CAL_TOOLS); 0 when off.
    tools_overhead: int
    compactor: TieredCompact | None
    spec: Spec | None
    spec_has_zero_calls: bool
    early_bail_message: str | None
    on_token: Callable[[str], None] | None
    on_progress: Callable[[float], None] | None
    on_notice: Callable[[str], None] | None
    on_tool_event: OnToolEvent | None

    def notice(self, text: str) -> None:
        _display(self.on_notice, text, "on_notice")

    def assistant(self, resp: ChatResponse, content: str) -> dict[str, Any]:
        """The assistant message the model's output becomes in `messages`.
        With LUXE_REASONING_ROUNDTRIP=1 it carries the reasoning the server
        split off, so the template renders the model's real `<think>` block
        instead of an empty one. Off: exactly the legacy dict."""
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if self.flags.reasoning_roundtrip and resp.reasoning_text:
            msg["reasoning_content"] = resp.reasoning_text
        return msg


def _step_window(run: _Run, st: LoopState, result: AgentResult,
                 step: int) -> tuple[int, float]:
    """This step's `(effective_ctx, pressure)`: the calibrated window every
    pressure consumer divides by, and the messages' pressure against it.
    Records peak/final pressure on `result` and reports it to `on_progress`.
    """
    role_cfg = run.role_cfg
    # The window every pressure consumer divides by this step. From step 2
    # on it is `num_ctx` shrunk by however far the chars/4 estimate ran
    # under the server's own `usage.prompt_tokens` last call — see
    # `calibrated_ctx_limit`. Step 1 has nothing to calibrate against and
    # uses `num_ctx` raw, which is the historical behaviour.
    # LUXE_CTX_CAL_DAMP (opt-in, default OFF): the live ratio was measured
    # on ONE prompt; apply only the share of it that prompt still covers.
    # `step_calibration is ctx_calibration` whenever the flag is off, so
    # `effective_ctx`, the debug line, and every consumer are unchanged.
    step_calibration = st.ctx_calibration
    if run.flags.ctx_cal_damp and st.ctx_calibration != 1.0:
        _est_now = estimate_messages_tokens(st.messages) + run.tools_overhead
        step_calibration = damped_calibration(
            st.ctx_calibration, st.est_at_calibration, _est_now,
            unmeasured=run.flags.ctx_cal_unmeasured_ratio)
        if step_calibration != st.ctx_calibration:
            logger.debug("ctx calibration damped %.2fx -> %.2fx "
                         "(measured at est=%d, prompt now est=%d)",
                         st.ctx_calibration, step_calibration,
                         st.est_at_calibration, _est_now)
    effective_ctx = calibrated_ctx_limit(role_cfg.num_ctx, step_calibration)
    if run.tools_overhead:
        # Consumers divide the MESSAGE estimate by this, so the schemas'
        # share of the window comes off the denominator: at a quotient of
        # 1.0 messages + tools fill the server's window exactly.
        effective_ctx = max(1, effective_ctx - run.tools_overhead)

    pressure = context_pressure(st.messages, effective_ctx)
    result.peak_context_pressure = max(result.peak_context_pressure, pressure)
    result.final_context_pressure = pressure  # instantaneous; matches token-progress
    # Per-step ctx forensics. `est` is the raw chars/4 reading, `cal` the
    # correction applied; when cal != 1.0 the reported pressure is
    # server-calibrated and should track chat's finalize_turn number rather
    # than sitting at half of it. debug.log-only; see logger note at top.
    logger.debug("step=%d ctx_pressure=%.1f%% (est=%.1f%% cal=%.2fx) "
                 "num_ctx=%d effective_ctx=%d msgs=%d",
                 step + 1, pressure * 100,
                 context_pressure(st.messages, role_cfg.num_ctx) * 100,
                 step_calibration, role_cfg.num_ctx, effective_ctx,
                 len(st.messages))
    # chat-only live ctx% (one source of truth, C2)
    _display(run.on_progress, pressure, "on_progress")
    return effective_ctx, pressure


def _compact_step(run: _Run, st: LoopState, effective_ctx: int, step: int,
                  emit: Callable[..., None]) -> int:
    """Compact `st.messages` for this step's request (TieredCompact, or the
    legacy elide path when it is off) and return `est_sent`, the estimate the
    server-truth calibration divides the server's prompt count by."""
    if run.compactor is not None:  # i.e. flags.tiered_compact
        cr = run.compactor.compact(st.messages, effective_ctx)
        st.messages = cr.messages
        if cr.phase_reached > 0:
            st.compaction_tool_results_dropped_total += cr.tool_results_dropped
            st.compaction_total_tokens_dropped += (cr.tokens_before - cr.tokens_after)
            if cr.phase_reached > st.compaction_max_phase_this_run:
                st.compaction_max_phase_this_run = cr.phase_reached
            # 2026-08-24 — ADDITIVE telemetry, no flag, no behaviour
            # change: a phase can fire and achieve nothing (phase 3 with
            # tokens_before == tokens_after == 71616 and zero drops, when
            # `_find_eligible_end` returned 2 because the turn had two
            # assistant messages). Existing field names and values are
            # untouched; `effective` and `eligible_end` say whether the
            # fire did anything and why. No events.jsonl consumer reads
            # compaction records today (checked: scripts/*.py,
            # benchmarks/maintain_suite/run.py, tests/) — the two new keys
            # cannot break a field-shape assumption that does not exist.
            if not cr.effective:
                st.compaction_ineffective_fires += 1
            emit(
                "compaction_phase_reached",
                step=step,
                phase_reached=cr.phase_reached,
                tokens_before=cr.tokens_before,
                tokens_after=cr.tokens_after,
                tool_results_dropped=cr.tool_results_dropped,
                effective=cr.effective,
                eligible_end=cr.eligible_end,
            )
    else:
        st.messages = elide_old_tool_results(st.messages, effective_ctx)

    # What we are about to send, measured the same way the calibration
    # divides it — AFTER compaction, so the ratio describes the request the
    # server actually answers.
    return estimate_messages_tokens(st.messages) + run.tools_overhead


def _adaptive_step(run: _Run, st: LoopState, step: int,
                   convergence_score: float,
                   emit: Callable[..., None]) -> None:
    """v1.11 — append to score_log + compute adaptive state. Called only
    under LUXE_ADAPTIVE_POLICY, to preserve disable-equivalence. The only
    dispatch consumer is WritePressureGuard's modulation, held at neutral
    1.0 by the pinned bias (flags.py, agents.sdd "Stage 3 / v1.11")."""
    st.score_log.append(convergence_score)
    # Bounded growth: cap at 64 entries (covers max_steps for current
    # configs with headroom). Drop oldest when over.
    if len(st.score_log) > 64:
        st.score_log[:] = st.score_log[-64:]
    adaptive_state = compute_within_run_state(
        st.score_log, st.tool_history, step,
        no_write_enabled=run.flags.adaptive_no_write,
        score_trend_enabled=run.flags.adaptive_score_trend,
    )
    # v1.11 Phase 3a — compute bias → target modulation → slew-rate-limited update.
    bias = compute_intervention_bias(adaptive_state)
    for kind, prev_mod in list(st.intervention_modulation.items()):
        target = bias_to_modulation(bias.get(kind, 0.0))
        st.intervention_modulation[kind] = apply_slew_rate(
            prev_mod, target, max_delta=run.flags.adaptive_max_delta,
        )
    emit(
        "adaptive_state",
        step=step,
        consecutive_no_write=adaptive_state.consecutive_no_write,
        score_trend=adaptive_state.score_trend,
        score_log_len=adaptive_state.score_log_len,
        convergence_score=convergence_score,
        modulation_write_pressure=st.intervention_modulation["write_pressure"],
        modulation_early_bail=st.intervention_modulation["early_bail"],
        modulation_soft_anchor=st.intervention_modulation["soft_anchor"],
    )


def _write_pressure(run: _Run, st: LoopState, result: AgentResult, step: int,
                    emit: Callable[..., None]) -> None:
    """Mid-loop write-pressure injection (Mode B fix). Fires once per
    run when the agent has done substantial reading + generation
    without writing. Targets the prose-mode trap where the model
    declares "comprehensive picture" prematurely and hallucinates
    the deliverable into chat instead of committing it. The
    synthetic user message interrupts the read-loop and forces a
    write decision before further tool calls accumulate.

    Decision logic lives in luxe.agents.guardrails.WritePressureGuard
    (forge-hybrid Phase 1). Loop owns state mutation (the *_fired flag,
    intervention tracking vars, event emission)."""
    wp_decision = WritePressureGuard.check(
        write_pressure_enabled=st.write_pressure_enabled,
        write_pressure_fired=st.write_pressure_fired,
        writes_seen=st.writes_seen,
        step=step,
        tool_calls_total=result.tool_calls_total,
        completion_tokens=result.completion_tokens,
        adaptive_policy_enabled=run.flags.adaptive_policy,
        intervention_modulation_write_pressure=st.intervention_modulation["write_pressure"],
    )
    if wp_decision is not None:
        st.messages.append(_user_message(wp_decision.message,
                                         WritePressureGuard.nudge_type))
        st.write_pressure_fired = True
        _record_intervention(st, step, "write_pressure")
        emit(
            "write_pressure_fired",
            step=step,
            tool_calls_total=result.tool_calls_total,
            completion_tokens=result.completion_tokens,
        )


def _early_bail(run: _Run, st: LoopState, result: AgentResult, step: int,
                convergence_score: float, emit: Callable[..., None]) -> None:
    """Early-bail intervention (v1.7 priority #1). Same checkpoint as
    write_pressure but fires earlier — at step 4 with 4+ non-write
    tool calls and zero writes. Trace-derived thresholds; see the
    block comment on guardrails._EARLY_BAIL_MIN_STEP for the empirical
    basis. The message gives the model a binary recovery gradient:
    edit OR explicitly decline-with-justification. Mutually
    compatible with WRITE_PRESSURE — both can fire in the same run
    since they target different trajectory shapes."""
    eb_outcome = EarlyBailGuard.evaluate(
        early_bail_enabled=st.early_bail_enabled,
        early_bail_fired=st.early_bail_fired,
        writes_seen=st.writes_seen,
        step=step,
        tool_calls_total=result.tool_calls_total,
        early_bail_message=run.early_bail_message,
        convergence_gate_enabled=st.convergence_gate_enabled,
        convergence_score=convergence_score,
        band_response=run.flags.early_bail_band_response,
        suppression_count_in_trajectory=st.suppression_count_in_trajectory,
        tool_history=st.tool_history,
        recent_path_diversity=recent_path_diversity(st.tool_history),
        early_bail_mode=run.flags.early_bail_mode,
    )
    if eb_outcome is None:
        return
    # Apply state mutations the loop owns. The guard tells us how
    # much to change suppression_count, whether to set
    # early_bail_fired, and which intervention-kind to record on the
    # trackers.
    st.suppression_count_in_trajectory += eb_outcome.suppression_count_delta
    if eb_outcome.sets_early_bail_fired:
        st.early_bail_fired = True
        st.early_bail_step = step
    if eb_outcome.decision is not None:
        st.messages.append(_user_message(eb_outcome.decision.message,
                                         eb_outcome.nudge_type))
    if eb_outcome.last_intervention_kind is not None:
        _record_intervention(st, step, eb_outcome.last_intervention_kind)
    # Suppression event first, then the fire event — the order the
    # records have always been written in.
    for event in (eb_outcome.suppress_event, eb_outcome.fire_event):
        if event is not None:
            ev_name, ev_payload = event
            # Loop owns the completion_tokens counter; fill it for
            # events that carry it (early_bail_fired).
            payload = {
                k: (result.completion_tokens if k == "completion_tokens" and v is None else v)
                for k, v in ev_payload.items()
            }
            emit(ev_name, step=step, **payload)


def _action_density_gate(st: LoopState, result: AgentResult, step: int,
                         convergence_score: float, action_density: float,
                         emit: Callable[..., None]) -> None:
    """v1.9 — LUXE_ACTION_DENSITY_GATE. Staged-escalation predicate that
    fires once per run in one of two modes:
      - standalone:        early_bail never fired; gate stands alone
      - post_bail_rescue:  early_bail fired ≥MIN_TURNS_AFTER_BAIL
                           turns ago AND no writes since
    Convergence proxy (same_file_read_twice on/before this step)
    suppresses the gate — strong trajectories converge by re-reading
    the same target. Thresholds derived from
    scripts/mine_action_density.py over v17 + v18 SWE-bench n=75;
    see acceptance/v19_mining/THRESHOLD_DECISION.md.
    v1.10 — convergence-score suppression replaces the v1.9 binary
    same_file_read_twice skip. When the gate is enabled AND the
    model has converged (score >= HIGH), suppress the gate — the
    rescue would interrupt a trajectory that's converging on its
    own. Keep the v1.9 same_file_read_twice_step as a fallback skip
    condition when the convergence gate is OFF (preserves v1.9
    ablation semantics)."""
    v110_suppress, v19_suppress = ActionDensityGateGuard.convergence_suppression(
        convergence_gate_enabled=st.convergence_gate_enabled,
        convergence_score=convergence_score,
        same_file_read_twice_step=st.same_file_read_twice_step,
        step=step,
    )
    adg_decision = ActionDensityGateGuard.check(
        action_density_gate_enabled=st.action_density_gate_enabled,
        action_density_gate_fired=st.action_density_gate_fired,
        writes_seen=st.writes_seen,
        step=step,
        completion_tokens=result.completion_tokens,
        tool_calls_total=result.tool_calls_total,
        v110_suppress=v110_suppress,
        v19_suppress=v19_suppress,
        early_bail_step=st.early_bail_step,
    )
    if adg_decision is not None:
        st.messages.append(_user_message(adg_decision.message,
                                         ActionDensityGateGuard.nudge_type))
        st.action_density_gate_fired = True
        _record_intervention(st, step, "action_density_gate")
        emit(
            "action_density_gate_fired",
            step=step,
            fire_mode=adg_decision.metadata["fire_mode"],
            turns_since_bail=adg_decision.metadata["turns_since_bail"],
            tool_calls_total=result.tool_calls_total,
            completion_tokens=result.completion_tokens,
            action_density=action_density,
            same_file_read_twice_step=st.same_file_read_twice_step,
            convergence_score=convergence_score,
        )
    else:
        # Observability — record the v1.10 suppression when the gate
        # would otherwise have fired, so post-hoc analysis can tell
        # convergence-suppression from threshold-miss.
        adg_suppressed = ActionDensityGateGuard.suppressed_converged(
            action_density_gate_enabled=st.action_density_gate_enabled,
            action_density_gate_fired=st.action_density_gate_fired,
            v110_suppress=v110_suppress,
            writes_seen=st.writes_seen,
            step=step,
            completion_tokens=result.completion_tokens,
            tool_calls_total=result.tool_calls_total,
            convergence_score=convergence_score,
        )
        if adg_suppressed is not None:
            emit(
                "action_density_gate_suppressed_converged",
                step=step,
                **adg_suppressed,
            )


def _action_density_sample(st: LoopState, result: AgentResult, step: int,
                           convergence_score: float, completion_delta: int,
                           action_density: float,
                           emit: Callable[..., None]) -> None:
    """Observability: emit action_density per step regardless of gating.
    Becomes the dataset for adaptive threshold tuning. Cheap. Not on step 0."""
    # v1.9 habituation telemetry: when an intervention has fired,
    # report (a) how many steps since it fired, (b) which one, and
    # (c) whether the immediately-following step produced any tool
    # call. Lets us post-hoc measure whether text-level interventions
    # remain causally active or accumulate as ignorable background.
    habituation: dict[str, Any] = {}
    if st.last_intervention_step is not None and step > st.last_intervention_step:
        step_had_call = result.tool_calls_total > st.prev_tool_calls_total_at_sample
        habituation = {
            "since_intervention_step": step - st.last_intervention_step,
            "since_intervention_kind": st.last_intervention_kind,
            "next_action_was_tool_call": step_had_call,
            # v1.10 — post-intervention behavior signals. None
            # until the first post-intervention write fires; once
            # it does, time_to_first_write_after_intervention is
            # fixed for the rest of the run. write_burst_persistence
            # is the running max consecutive post-intervention
            # writes — captures "stuck on cleanup" vs "real
            # engagement" once the model commits.
            "time_to_first_write_after_intervention":
                st.first_write_step_after_intervention,
            "write_burst_persistence": st.post_intervention_write_burst_max,
        }
    # v1.10 — convergence_score is already computed at top of
    # step (used by early_bail + action_density_gate predicates);
    # just emit it on the sample event for observability.
    emit(
        "action_density_sample",
        step=step,
        completion_delta=completion_delta,
        action_density=action_density,
        writes_seen=st.writes_seen,
        tool_calls_total=result.tool_calls_total,
        convergence_score=convergence_score,
        **habituation,
    )


def _pre_step_guards(run: _Run, st: LoopState, result: AgentResult, step: int,
                     resp: ChatResponse | None,
                     emit: Callable[..., None]) -> bool:
    """Everything that runs at the top of a step, before compaction and the
    request: the adaptive substrate, the three nudge interventions, the
    habituation clean exit, and the per-step sample. Returns True when the
    run must END here (habituation exit; `result.final_text` is set).
    `resp` is the PREVIOUS step's response (None on step 0)."""
    # v1.10 — compute convergence score ONCE per step at the top of the
    # iteration. Used by both early_bail and action_density_gate
    # predicates AND emitted on the action_density_sample observability
    # event. Pure function over the bounded tool_history; cheap to
    # evaluate every step. The score is the v1.10 replacement for the
    # v1.9 binary `same_file_read_twice_step` skip — see
    # luxe.agents.convergence module docstring for the design rationale.
    convergence_score = compute_convergence_score(st.tool_history)
    if run.flags.adaptive_policy:
        _adaptive_step(run, st, step, convergence_score, emit)

    _write_pressure(run, st, result, step, emit)
    _early_bail(run, st, result, step, convergence_score, emit)

    # Per-step deltas (v1.8 Track 1 plumbing). Used by
    # action_density_gate and the action_density_sample observability
    # event. completion_delta_last_step is the SIZE of the previous
    # step's response — we evaluate at the start of step N to catch a
    # step N-1 burst, leaving budget for the intervention to land.
    completion_delta_last_step = result.completion_tokens - st.prev_completion_tokens
    action_density = (
        (result.tool_calls_total / max(1, result.completion_tokens))
        if result.completion_tokens > 0 else 0.0
    )
    _action_density_gate(st, result, step, convergence_score, action_density,
                         emit)

    # v1.10.1 — habituation clean-exit. When ≥3 distinct interventions
    # have fired this run AND the model has produced ZERO post-intervention
    # writes AND step ≥ _HABITUATION_EXIT_MIN_STEP, the trajectory is
    # intervention-resistant. Burning the remaining max_steps budget
    # yields no further information. Exit cleanly to preserve trace +
    # evaluation semantics (mirrors the post_write_idle_exit shape). Founding instance: sympy-13031 fired
    # all three distinct interventions by step 15, zero writes through
    # max_steps. `resp` is from the prior iteration's backend.chat call.
    hab_exit = HabituationExitGuard.should_exit(
        intervention_kinds_fired=st.intervention_kinds_fired,
        first_write_step_after_intervention=st.first_write_step_after_intervention,
        step=step,
        last_intervention_step=st.last_intervention_step,
        tool_calls_total=result.tool_calls_total,
        completion_tokens=result.completion_tokens,
    )
    if hab_exit is not None:
        result.final_text = (resp.text if resp else "") or ""
        emit(
            "habituation_exit",
            step=step,
            **hab_exit,
        )
        return True

    if step > 0:
        _action_density_sample(st, result, step, convergence_score,
                               completion_delta_last_step, action_density,
                               emit)
    st.prev_tool_calls_total_at_sample = result.tool_calls_total
    # Capture cumulative tokens BEFORE this step's backend.chat so the
    # next iteration's delta correctly measures THIS step's response.
    st.prev_completion_tokens = result.completion_tokens
    return False


def _account_usage(result: AgentResult, resp: ChatResponse, step: int,
                   emit: Callable[..., None]) -> None:
    """Fold one response's server-reported usage into `result` and write the
    step's `step_usage` record."""
    result.prompt_tokens += resp.timing.prompt_tokens
    result.completion_tokens += resp.timing.completion_tokens
    if resp.timing.prompt_tokens:
        result.last_prompt_tokens = resp.timing.prompt_tokens
    _t = resp.timing
    if _t.cached_tokens is not None:
        result.cached_tokens += _t.cached_tokens
        result.perf_prompt_tokens += _t.prompt_tokens
    if _t.prefill_s:
        result.prefill_s += _t.prefill_s
        result.prefill_tokens += max(_t.prompt_tokens - (_t.cached_tokens or 0), 0)
    if _t.decode_s:
        result.decode_s += _t.decode_s
        result.decode_tokens += _t.completion_tokens
    if step == 0:
        result.first_step_prompt_tokens = _t.prompt_tokens
        result.first_step_cached_tokens = _t.cached_tokens
    # Per-step cache/prefill record (2026-10-15, additive telemetry): the
    # only place a run's prefix-cache behaviour is visible client-side.
    emit(
        "step_usage",
        step=step,
        prompt_tokens=_t.prompt_tokens,
        cached_tokens=_t.cached_tokens,
        completion_tokens=_t.completion_tokens,
        prefill_s=_t.prefill_s,
        decode_s=_t.decode_s,
        reasoning_chars=resp.reasoning_chars,
    )
    if resp.timing.cost_usd:
        # SUM, not last-wins: a multi-step turn bills once per step, and a
        # cap that only ever saw the final step's cost would undercount by
        # the length of the turn.
        result.cost_usd += resp.timing.cost_usd


def _recalibrate(st: LoopState, resp: ChatResponse, est_sent: int) -> None:
    """Server-truth calibration: re-measure the ratio from the response we
    just got. Re-measured every step rather than latched once: the mix shifts
    as tool payloads accumulate, and a run that starts on prose and ends on
    JSON should not keep the first step's ratio. `calibration_ratio` clamps
    and degrades to 1.0 on a missing/zero usage report."""
    new_cal = calibration_ratio(resp.timing.prompt_tokens, est_sent)
    if new_cal != st.ctx_calibration:
        logger.debug("ctx calibration %.2fx -> %.2fx "
                     "(server=%d est=%d)", st.ctx_calibration, new_cal,
                     resp.timing.prompt_tokens, est_sent)
    st.ctx_calibration = new_cal
    # The prompt this ratio describes. Only meaningful while the
    # ratio is; read solely by the opt-in damping in _step_window.
    st.est_at_calibration = est_sent


def _token_progress(st: LoopState, result: AgentResult, step: int,
                    pressure: float) -> None:
    """Token-interval progress logging — fires when cumulative completion
    tokens crosses each LUXE_TOKEN_LOG_INTERVAL multiple. Lets us see
    whether a model is steadily generating with tool calls (engaged) vs
    bursting prose without tools (bailing)."""
    if (st.next_token_log_threshold > 0
            and result.completion_tokens >= st.next_token_log_threshold):
        print(
            f"    [token-progress] step={step+1} "
            f"completion_tokens={result.completion_tokens} "
            f"prompt_tokens={result.prompt_tokens} "
            f"tool_calls={result.tool_calls_total} "
            f"ctx_pressure={pressure:.0%}",
            flush=True,
        )
        while st.next_token_log_threshold <= result.completion_tokens:
            st.next_token_log_threshold += _TOKEN_LOG_INTERVAL


def run_agent(
    backend: Backend,
    role_cfg: RoleConfig,
    *,
    system_prompt: str,
    task_prompt: str,
    tool_defs: list[ToolDef],
    tool_fns: dict[str, ToolFn],
    cache: object | None = None,
    cacheable: set[str] | None = None,
    on_tool_event: OnToolEvent | None = None,
    run_id: str | None = None,
    phase: str = "main",
    spec: Spec | None = None,
    early_bail_message: str | None = None,
    on_token: Callable[[str], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
    on_notice: Callable[[str], None] | None = None,
    prior_messages: list[dict[str, Any]] | None = None,
) -> AgentResult:
    """Run the agent loop: chat → tool calls → dispatch → repeat.

    `spec` (v1.7) enables SpecDD Lever 1 reprompt gating. Two
    agent-trajectory predicates are supported via spec_validator:
      - expects_zero_calls: PRE-DISPATCH gate (v1.8 Track 2) — drops the
        tool call before dispatch_tool runs; injects a decline reprompt.
        Suppresses write_pressure + early_bail (tool-eagerness amplifiers).
      - min_tool_calls: reprompts at loop-break if the model produced
        fewer than min_matches calls; resumes the loop. Fires at most
        once per requirement.

    `early_bail_message` (v1.8 Track 3) overrides the default
    `_EARLY_BAIL_MESSAGE` for this run; it takes precedence over
    `LUXE_EARLY_BAIL_MODE`. No production caller passes it today — the
    SWE-bench adapter selects its variant through the env var instead
    (`LUXE_EARLY_BAIL_MODE=soft_anchor`), and maintain_suite uses the
    default (abstain is sometimes a legitimate outcome there). Pass None
    to use the default.

    `on_notice` (2026-08-11) receives one-line, human-facing statements about
    the loop acting on its own — today, the truncated-turn retry. It exists
    because that mechanism can spend several minutes of a chat turn (a full
    capped generation per retry) while the UI shows nothing but a spinner:
    the only record was `events.jsonl`, which nobody reads mid-turn. Display
    only. It never influences control flow, is never consulted for a decision,
    and defaults to None, so the benchmark/maintain path is unchanged.

    `prior_messages` (2026-10-16, chat-only) continues an earlier run's
    conversation: the run starts from those messages plus `task_prompt` as a
    new user message, instead of a fresh `[system, user]` pair, so the prompt
    EXTENDS the previous run's last request and the server's prefix cache
    resumes from it. `prior_messages[0]` must be the same system message; a
    mismatch is ignored (fresh start, `turn_carry_rejected` event). None —
    every benchmark/maintain caller — is the unchanged fresh start.

    `cache` / `cacheable` are ACCEPTED AND IGNORED (deprecated 2026-09). They
    fed a per-task `ToolCache` that no caller ever constructed, so every run
    already dispatched uncached; the cache was removed but the keywords stay
    because `run_agent`'s signature is frozen for its callers (chat.sdd
    Must-not). Passing them has no effect.
    """

    result = AgentResult()
    t0 = time.monotonic()

    # Every LUXE_* switch this run obeys, read once, here (agents/flags.py).
    # The body reads `flags.<field>` directly; flags.py documents each one.
    flags = RunFlags.from_env()
    # v1.10.1 — log_calls default-on when run_id is set. Earlier policy
    # was opt-in via LUXE_LOG_TOOL_CALLS=1, which silently degraded the
    # v1.10 production taxonomy (intervention fires + tool_calls invisible)
    # for any run that didn't have the env exported. Default-on closes
    # the footgun the v1.10 audit caught. Opt out via LUXE_SUPPRESS_TOOL_LOG=1
    # (ablation parity for legacy callers).
    log_calls = bool(run_id) and not flags.suppress_tool_log

    # Every events.jsonl record this run writes goes through `emit`, which
    # owns the `log_calls` gate and the leading `phase=` field.
    emit = _event_emitter(run_id, phase, log_calls)

    openai_tools = [td.to_openai() for td in tool_defs] if tool_defs else None
    # LUXE_CTX_CAL_TOOLS (default-ON 2026-10-20): the tool schemas ride in every
    # request but not in `messages`, so the server-truth ratio was measured
    # against an estimate that never saw them — ~7x at step 0 with a 320-token
    # message list — and then multiplied onto the next file read. Counted here,
    # they join the estimate the ratio divides and leave the window as a fixed
    # charge. 0 when disabled (=0), which restores every number below exactly.
    tools_overhead = (estimate_tools_tokens(openai_tools)
                      if flags.ctx_cal_tools else 0)
    tool_def_map = {td.name: td for td in tool_defs}
    known_names = set(tool_def_map.keys())

    # All mutable loop state (agents/loopstate.py). Field-by-field the same
    # names and initial values the bare locals had.
    initial_messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_prompt},
    ]
    if prior_messages:
        if prior_messages[0] == initial_messages[0]:
            initial_messages = [*prior_messages, initial_messages[1]]
            result.carried = True
            emit("turn_carry", prior_messages=len(prior_messages))
        else:
            emit("turn_carry_rejected", reason="system_prompt_changed")
    st = LoopState(
        messages=initial_messages,
        next_token_log_threshold=_TOKEN_LOG_INTERVAL,  # 0 = disabled
        write_pressure_enabled=flags.write_pressure,
        early_bail_enabled=flags.early_bail,
        action_density_gate_enabled=flags.action_density_gate,
        convergence_gate_enabled=flags.convergence_gate,
    )

    # forge-hybrid Phase 2 (A) — TieredCompact context compaction, DEFAULT-ON
    # since 2026-05-28 (agents.sdd). None = the legacy elide path.
    compactor: TieredCompact | None = (
        TieredCompact(
            compact_threshold=flags.tiered_compact_threshold,
            phase_thresholds=flags.tiered_compact_phase_thresholds,
            cache_friendly=flags.compact_cache_friendly,
        ) if flags.tiered_compact else None
    )

    spec_has_zero_calls = (
        spec is not None
        and any(r.kind == "expects_zero_calls" for r in spec.requirements)
    )
    if spec_has_zero_calls:
        # Suppression: the three tool-eagerness amplifiers (write_pressure,
        # early_bail, action_density_gate) would push the
        # model toward action exactly when the correct outcome is to
        # decline. Disable all when the spec contains a zero-call
        # expectation. v1.10 convergence_gate has no effect when the
        # gated interventions are themselves disabled, but we mirror the
        # off-switch for clarity.
        st.write_pressure_enabled = False
        st.early_bail_enabled = False
        st.action_density_gate_enabled = False
        st.convergence_gate_enabled = False
    run = _Run(
        role_cfg=role_cfg, flags=flags,
        tool_defs=tool_defs, tool_fns=tool_fns,
        tool_def_map=tool_def_map, known_names=known_names,
        openai_tools=openai_tools, tools_overhead=tools_overhead,
        compactor=compactor,
        spec=spec, spec_has_zero_calls=spec_has_zero_calls,
        early_bail_message=early_bail_message,
        on_token=on_token, on_progress=on_progress, on_notice=on_notice,
        on_tool_event=on_tool_event,
    )
    # The previous iteration's response. Bound here (not just inside the loop)
    # because the pre-step clean-exit guards below read it before this step's
    # backend.chat runs; on step 0 there is nothing to read and `""` is right.
    # Was a latent NameError + a `'resp' in dir()` probe (ruff F821, 2026-07-29).
    resp: ChatResponse | None = None

    for step in range(role_cfg.max_steps):
        result.steps = step + 1

        effective_ctx, pressure = _step_window(run, st, result, step)

        if _pre_step_guards(run, st, result, step, resp, emit):
            break

        est_sent = _compact_step(run, st, effective_ctx, step, emit)

        try:
            resp = backend.chat(
                st.messages,
                tools=openai_tools,
                max_tokens=role_cfg.max_tokens_per_turn,
                temperature=role_cfg.temperature,
                num_ctx=role_cfg.num_ctx,
                repeat_penalty=role_cfg.repeat_penalty,
                # Chat front-end only: stream tokens for the live tail. None
                # (benchmark/maintain) → stream=False → byte-identical request.
                stream=on_token is not None,
                on_token=on_token,
            )
        except Exception as e:
            result.aborted = True
            result.abort_reason = f"Backend error: {e}"
            break

        _account_usage(result, resp, step, emit)
        if flags.ctx_server_truth:
            _recalibrate(st, resp, est_sent)
        _token_progress(st, result, step, pressure)

        tool_calls = resp.tool_calls
        if not tool_calls and resp.text and tool_defs:
            text_drops: list[str] = []
            tool_calls = _parse_text_tool_calls(resp.text, known_names,
                                                drops=text_drops)
            if text_drops:
                logger.warning(
                    "text-fallback dropped unknown tool call(s): %s",
                    [d[:80] for d in text_drops])
                emit(
                    "textfallback_drop",
                    step=step,
                    names=[d[:80] for d in text_drops],
                    recovered=bool(tool_calls),
                )

        if not tool_calls:
            # Truncated-turn gate (2026-08-10). A response cut off at
            # max_tokens comes back finish_reason="length"; mid-prose it also
            # carries no tool calls, and the terminal test below cannot tell
            # that from a model that finished and chose to answer. Nudge and
            # continue instead of ending a run that never acted. Bounded, and
            # a no-op unless the switch is on — see agents.sdd.
            tt = TruncatedTurnGuard.should_fire(
                truncated_turn_retry_enabled=flags.truncated_turn_retry,
                finish_reason=getattr(resp, "finish_reason", "") or "",
                has_tool_calls=bool(tool_calls),
                retries_used=st.truncated_turn_retries_used,
                max_retries=flags.truncated_turn_max_retries,
            )
            if tt is not None:
                # Record the cut-off text before the nudge so the transcript
                # shows what the model was mid-way through saying.
                if resp.text:
                    st.messages.append(run.assistant(resp, resp.text))
                st.messages.append(_user_message(_TRUNCATED_TURN_MESSAGE,
                                                 TruncatedTurnGuard.nudge_type))
                st.truncated_turn_retries_used += 1
                emit(
                    "truncated_turn_retry",
                    step=step,
                    retries_used=st.truncated_turn_retries_used,
                    max_retries=flags.truncated_turn_max_retries,
                    completion_tokens=resp.timing.completion_tokens,
                )
                logger.debug(
                    "truncated turn retry step=%d retries_used=%d max=%d "
                    "completion_tokens=%d",
                    step, st.truncated_turn_retries_used,
                    flags.truncated_turn_max_retries, resp.timing.completion_tokens)
                run.notice(
                    f"answer cut off at the {resp.timing.completion_tokens:,}-token "
                    f"cap with no tool call — retrying "
                    f"({st.truncated_turn_retries_used}/{flags.truncated_turn_max_retries}); "
                    f"each retry costs another full generation"
                )
                continue

            # Empty-completion gate (2026-08-17). A turn with no tool call AND
            # no text satisfies the terminal test above exactly as a real
            # answer does, so a blank reply was recorded as a finished turn —
            # no gate, no warning, nothing in the record to tell them apart.
            # Nudge and continue. Bounded at one retry; ordered AFTER the
            # truncation gate so a cut-off response is handled by the guard
            # that owns it rather than spending both budgets on one problem.
            et = EmptyTurnGuard.should_fire(
                empty_turn_retry_enabled=flags.empty_turn_retry,
                text=resp.text or "",
                has_tool_calls=bool(tool_calls),
                finish_reason=getattr(resp, "finish_reason", "") or "",
                retries_used=st.empty_turn_retries_used,
            )
            if et is not None:
                st.messages.append(_user_message(_EMPTY_TURN_MESSAGE,
                                                 EmptyTurnGuard.nudge_type))
                st.empty_turn_retries_used += 1
                reasoning_chars = getattr(resp, "reasoning_chars", 0)
                emit(
                    "empty_turn_retry",
                    step=step,
                    retries_used=st.empty_turn_retries_used,
                    max_retries=et["max_retries"],
                    finish_reason=et["finish_reason"],
                    reasoning_chars=reasoning_chars,
                )
                logger.debug(
                    "empty turn retry step=%d retries_used=%d "
                    "finish_reason=%s reasoning_chars=%d",
                    step, st.empty_turn_retries_used, et["finish_reason"],
                    reasoning_chars)
                # Say WHY when we can: "it thought and said nothing" is a very
                # different report from "it returned nothing", and the
                # reasoning-channel count is the only thing that separates them.
                run.notice(
                    "the model replied with nothing"
                    + (f" after {reasoning_chars:,} characters of reasoning"
                       if reasoning_chars else "")
                    + " — retrying once"
                )
                continue

            # SpecDD Lever 1 min_tool_calls gate: before declaring the run
            # finished, check whether the spec expects more tool calls than
            # the model has emitted. If so, inject a reprompt and continue
            # the loop instead of breaking. Each requirement fires at most
            # once per run, so a stuck model can't ping-pong forever.
            if spec is not None and tool_defs:
                vr = spec_validate(spec, "", "", tool_calls=st.actual_tool_calls)
                reprompts: list[str] = []
                for rr in vr.unsatisfied:
                    if rr.requirement.id in st.spec_violations_reprompted:
                        continue
                    if rr.requirement.kind != "min_tool_calls":
                        continue
                    reprompts.append(rr.detail)
                    st.spec_violations_reprompted.add(rr.requirement.id)
                    emit(
                        "spec_reprompt_fired",
                        step=step,
                        requirement_id=rr.requirement.id,
                        requirement_kind=rr.requirement.kind,
                    )
                if reprompts:
                    # Replay the assistant's would-be final answer FIRST, then
                    # the reprompt, so the model reads the correction after
                    # its own exit. Appending in the other order (the pre-2026-09
                    # behavior) ended the request on the stale answer, with the
                    # correction buried above it.
                    if resp.text:
                        st.messages.append(run.assistant(resp, resp.text))
                    for detail in reprompts:
                        st.messages.append(_user_message(detail))
                    continue
            # TELEMETRY ONLY — additive, ungated, never touches control flow or
            # `messages` (agents.sdd "Tool-call telemetry events"). A run that
            # ends here on finish_reason="length" was CUT OFF mid-answer, not
            # finished; without this the records cannot tell the two apart,
            # which is how the strict-flag fixture read as a clean completion
            # for six identical runs.
            if (getattr(resp, "finish_reason", "") or "") == "length":
                emit(
                    "terminal_turn_truncated",
                    step=step,
                    completion_tokens=resp.timing.completion_tokens,
                    final_text_chars=len(resp.text or ""),
                    retry_enabled=flags.truncated_turn_retry,
                    retries_used=st.truncated_turn_retries_used,
                )
            # Same reason, the other failure: a run that ends HERE with no text
            # produced no answer at all. Ungated telemetry — this is the only
            # record distinguishing "answered" from "said nothing", exactly as
            # `terminal_turn_truncated` is for "finished" vs "cut off".
            if not (resp.text or "").strip():
                emit(
                    "terminal_turn_empty",
                    step=step,
                    finish_reason=getattr(resp, "finish_reason", "") or "",
                    reasoning_chars=getattr(resp, "reasoning_chars", 0),
                    completion_tokens=resp.timing.completion_tokens,
                    retry_enabled=flags.empty_turn_retry,
                    retries_used=st.empty_turn_retries_used,
                )
            if (getattr(resp, "finish_reason", "") or "") == "length":
                run.notice(
                    f"answer cut off at the {resp.timing.completion_tokens:,}-token "
                    f"cap — ending the turn "
                    + (f"({st.truncated_turn_retries_used} retr"
                       + ("y" if st.truncated_turn_retries_used == 1 else "ies")
                       + " already used)"
                       if st.truncated_turn_retries_used
                       else "without retrying")
                )
            result.final_text = resp.text
            break

        # SpecDD Lever 1 PRE-DISPATCH spec gate (v1.8 Track 2). When the
        # spec contains any `expects_zero_calls` requirement and the model
        # has emitted a tool call, we intercept BEFORE dispatch_tool runs:
        # do NOT add anything to actual_tool_calls (the grader checks
        # len(actual_tool_calls) == 0), do NOT execute the call, replay
        # the assistant text without the tool_calls field, then inject a
        # decline reprompt and continue the loop. This is capability
        # gating, not post-hoc policy auditing — the bench grades on
        # executed behavior, so the runtime must enforce before dispatch.
        # See plan §C.2 latency contract and lessons.md 2026-05-12 entry.
        if spec_has_zero_calls and tool_calls:
            # Strip tool_calls from the assistant message so the model's
            # next turn doesn't see a dangling "I tried to call X" without
            # a corresponding tool result.
            assistant_text = resp.text or ""
            st.messages.append(run.assistant(resp, assistant_text))
            st.messages.append(_user_message(_SPEC_ZERO_CALLS_DECLINE_MESSAGE))
            emit(
                "spec_predispatch_blocked",
                step=step,
                blocked_tool_names=[tc.name for tc in tool_calls],
                blocked_count=len(tool_calls),
            )
            # Skip the dispatch loop entirely. Tool calls are dropped on
            # the floor — they never enter actual_tool_calls, so the BFCL
            # grader sees zero calls. Continue to next step.
            continue

        # INTERMEDIATE PROSE (2026-08-17). This step is acting, not finishing,
        # yet models routinely put the lead-in to their answer here ("Let me
        # check X…", or an opening paragraph) and only the LAST step's text
        # becomes `final_text`. Chat showed that last fragment alone, so a
        # reply could start mid-thought (session 0eb5998d8825 turn -7: 3 steps,
        # 2 tool calls, the answer's opening lived in step 1). Recorded as a
        # SEPARATE field: `final_text` keeps its exact meaning and no benchmark
        # grader sees anything new. Only the chat layer reads this.
        if (resp.text or "").strip():
            result.step_texts.append(resp.text)
        assistant_msg: dict[str, Any] = run.assistant(resp, resp.text or "")
        if resp.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id or f"call_{step}_{i}",
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for i, tc in enumerate(resp.tool_calls)
            ]
        st.messages.append(assistant_msg)

        step_had_repeat = False
        for tc in tool_calls:
            result.tool_calls_total += 1
            # Normalize the tool name at the loop boundary, not just inside
            # dispatch_tool. Several downstream checks (`_WRITE_TOOLS`,
            # `_DEDUP_EXEMPT_TOOLS`, `tool_def_map`, `_call_key`) all compare
            # against the raw name; if GLM emits "edit_file\n", every one of
            # them misses and bookkeeping silently drifts (writes_seen never
            # increments → WP fires after diffs already landed,
            # post_write_idle_exit never arms).
            tc.name = tc.name.strip()

            # Arguments that decode to something other than an object (`null`,
            # a list, a double-encoded string) used to reach `validate_args` /
            # `_call_key` / `.get` and kill the whole run with an uncaught
            # AttributeError. Reject them like any other schema error — for
            # every name, offered or not.
            if not isinstance(tc.arguments, dict):
                err = ("Arguments must be a JSON object, got "
                       f"{type(tc.arguments).__name__}")
            elif tc.name in tool_def_map:
                err = validate_args(tool_def_map[tc.name], tc.arguments)
            else:
                err = None
            if err:
                result.schema_rejects += 1
                emit(
                    "tool_reject",
                    step=step, name=tc.name,
                    reason="schema", message=str(err)[:300],
                )
                st.messages.append(_tool_message(tc, step, f"Schema error: {err}"))
                continue

            key = _call_key(tc.name, tc.arguments)
            key_hash = hashlib.sha1(key.encode()).hexdigest()[:8]
            # v1.9 — convergence proxy. The first time a read_file call key
            # repeats (same path + args), record the step. Strong trajectories
            # re-read targets ~3× more often than empties; this is a
            # "found my target" signal that suppresses the action-density
            # gate. Tracked for read_file only — repeating a search/edit has
            # different semantics (revising, not converging on a candidate).
            if tc.name == "read_file":
                if key in st.read_keys_seen and st.same_file_read_twice_step is None:
                    st.same_file_read_twice_step = step
                else:
                    st.read_keys_seen.add(key)
            if key in st.seen_calls and tc.name not in _DEDUP_EXEMPT_TOOLS:
                step_had_repeat = True
                content = dedup_message(tc.name)
                dup = ToolCall(
                    id=tc.id or f"call_{step}",
                    name=tc.name,
                    arguments=tc.arguments,
                    result=content,
                    cached=True,
                    duplicate=True,
                    bytes_out=0,
                    wall_s=0.0,
                )
                result.tool_calls.append(dup)
                st.messages.append(_tool_message(tc, step, content))
                _display(run.on_tool_event, dup, "on_tool_event")
                emit(
                    "tool_call",
                    step=step, name=tc.name,
                    key_hash=key_hash, duplicate=True, cached=False,
                    bytes_out=0,
                )
                if st.writes_seen > 0:
                    st.post_write_idle_tools += 1
                continue

            # Dispatch-time visibility: every other record (events.jsonl,
            # on_tool_event, /verbose) fires on COMPLETION, so a hung tool
            # was invisible everywhere — including post-hoc (session
            # 5bb630813c21: 9m40s on a curl with no trace of the command).
            logger.debug("tool dispatch step=%d name=%s args=%s",
                         step + 1, tc.name, _args_preview(tc.arguments))
            executed = dispatch_tool(tc.name, tc.arguments, tool_fns)
            logger.debug("tool done name=%s wall_s=%.2f error=%s bytes_out=%d",
                         tc.name, getattr(executed, "wall_s", 0.0) or 0.0,
                         (getattr(executed, "error", None) or "")[:200] or None,
                         getattr(executed, "bytes_out", 0) or 0)
            if executed.error and executed.error.startswith("Unknown tool"):
                emit(
                    "tool_reject",
                    step=step, name=tc.name,
                    reason="unknown_tool", message=executed.error[:300],
                )
            result.tool_calls.append(executed)
            # SpecDD Lever 1: track every successfully-dispatched call (name,
            # args) for the spec validator. Skip error and schema-reject
            # cases — those don't represent a "real" call as far as the
            # agent-trajectory predicates are concerned.
            if not executed.error:
                st.actual_tool_calls.append((tc.name, tc.arguments))
            st.seen_calls.add(key)
            # v1.10 — append to tool_history for the convergence score.
            # Bounded to the last TOOL_HISTORY_MAX entries; only
            # successfully-dispatched calls (errors don't represent observed
            # behavior). Path extraction is permissive — see
            # luxe.agents.convergence.extract_path.
            if not executed.error:
                st.tool_history.append({
                    "step": step,
                    "name": tc.name,
                    "path": extract_path(tc.name, tc.arguments),
                })
                if len(st.tool_history) > TOOL_HISTORY_MAX:
                    st.tool_history = st.tool_history[-TOOL_HISTORY_MAX:]
            if tc.name in _WRITE_TOOLS and not executed.error:
                st.writes_seen += 1
                st.post_write_idle_tools = 0
                # A write changes what every observation tool would return, so
                # "you already called this — the result was provided above" is
                # false for any call made before it. Keyed on args alone, the
                # dedup refused post-write re-verification (`bash` pytest,
                # `grep`, `typecheck`): 174 such refusals across 122 runs in
                # ~/.luxe/runs (2026-09 review), each also counted toward the
                # repeat-streak abort. Write keys survive, so a repeated
                # identical write is still deduplicated as before.
                st.seen_calls = {k for k in st.seen_calls
                                 if k.partition(":")[0] in _WRITE_TOOLS}
                # forge-hybrid Phase 2 (A) — capture compaction phase at the
                # first successful write. Used by the resolve-time telemetry
                # to attribute write-step gating to compaction state. Fires
                # at most once per run.
                if (flags.tiered_compact
                        and st.compaction_phase_at_first_write is None):
                    st.compaction_phase_at_first_write = st.compaction_max_phase_this_run
                    emit(
                        "compaction_phase_at_first_write",
                        step=step,
                        phase_reached=st.compaction_phase_at_first_write,
                    )
                # v1.10 — post-intervention write telemetry. Capture
                # time-to-first-write and sustained-write-burst signals
                # for any trajectory where an intervention fired earlier.
                if st.last_intervention_step is not None:
                    if st.first_write_step_after_intervention is None:
                        st.first_write_step_after_intervention = step - st.last_intervention_step
                    st.post_intervention_consecutive_writes += 1
                    if st.post_intervention_consecutive_writes > st.post_intervention_write_burst_max:
                        st.post_intervention_write_burst_max = st.post_intervention_consecutive_writes
            elif st.writes_seen > 0:
                # A dedup-exempt repeat (read_file) returns content, so it
                # RESETS the streak. Counting such repeats as idle was benched
                # twice and refuted (LUXE_POST_WRITE_IDLE_REPEATS, removed
                # 2026-10-22; agents.sdd "Removed mechanisms").
                idle = executed.bytes_out == 0 or executed.error
                if idle:
                    st.post_write_idle_tools += 1
                else:
                    st.post_write_idle_tools = 0
                # v1.10 — non-write after intervention breaks the burst
                # (only matters once at least one write has occurred).
                if st.last_intervention_step is not None:
                    st.post_intervention_consecutive_writes = 0

            content = executed.error or executed.result
            # LUXE_TOOL_RESULT_CLAMP (opt-in, default OFF). Bound ONE result
            # here, at creation, rather than asking TieredCompact to reach a
            # message its pinned protections put out of reach. Off => the
            # helper is not called at all and `content` is the untouched
            # string, so the benchmark path is byte-identical by construction.
            #
            # `executed.bytes_out` and the `tool_call` event keep reporting
            # what the TOOL produced — the clamp is about what the CONTEXT
            # carries, and conflating the two would make the corpus lie about
            # tool behaviour.
            #
            # The other two `role: "tool"` appends in this loop (the schema
            # reject and the dedup short-circuit) are deliberately NOT
            # clamped: neither runs `dispatch_tool`, and both carry loop-
            # authored constants — `validate_args` returns a fixed sentence
            # that never echoes an argument value, and the dedup message is
            # literal. Clamping them could only ever be a no-op on a hot path.
            if flags.tool_result_clamp and content:
                _clamp_max = tool_result_clamp_chars(effective_ctx)
                content, _clamp_dropped = clamp_tool_result(
                    content, tool_name=tc.name, max_chars=_clamp_max,
                    path=extract_path(tc.name, tc.arguments),
                )
                if _clamp_dropped:
                    st.tool_results_clamped += 1
                    logger.debug("tool result clamped name=%s max_chars=%d "
                                 "dropped=%d", tc.name, _clamp_max,
                                 _clamp_dropped)
                    emit(
                        "tool_result_clamped",
                        step=step, name=tc.name,
                        max_chars=_clamp_max,
                        chars_dropped=_clamp_dropped,
                        bytes_out=executed.bytes_out,
                    )
            st.messages.append(_tool_message(tc, step, content))

            _display(run.on_tool_event, executed, "on_tool_event")
            emit(
                "tool_call",
                step=step, name=tc.name,
                key_hash=key_hash, duplicate=False,
                cached=executed.cached, bytes_out=executed.bytes_out,
                # v1.10 — emit path arg so future convergence-score
                # mining can run against the trace. Cheap; falls
                # back to None for tools without a path-like arg.
                path=extract_path(tc.name, tc.arguments),
            )
            # forge-hybrid Phase 2 (A) — post-compact recovery markers.
            # Emits one of three event types whenever a recovery-class
            # tool runs after compaction has fired anywhere in the run.
            # Used to characterize whether compaction is followed by
            # productive read/grep/edit activity (the search-geometry
            # signal flagged in the plan's risk register).
            if st.compaction_max_phase_this_run > 0:
                recovery_event = _COMPACTION_RECOVERY_EVENT_BY_TOOL.get(tc.name)
                if recovery_event is not None:
                    emit(
                        recovery_event,
                        step=step, name=tc.name,
                        compaction_max_phase=st.compaction_max_phase_this_run,
                    )

        pwi_exit = PostWriteIdleExitGuard.should_exit(
            post_write_idle_tools=st.post_write_idle_tools,
            writes_seen=st.writes_seen,
        )
        if pwi_exit is not None:
            result.final_text = resp.text or ""
            emit(
                "post_write_idle_exit",
                step=step,
                **pwi_exit,
            )
            break

        if step_had_repeat:
            st.consecutive_repeat_steps += 1
            emit(
                "tool_step_done",
                step=step,
                step_had_repeat=True,
                consecutive_repeat_steps=st.consecutive_repeat_steps,
            )
            cr_abort = ConsecutiveRepeatGuard.should_abort(
                consecutive_repeat_steps=st.consecutive_repeat_steps,
            )
            if cr_abort is not None and not role_cfg.stuck_loop_abort:
                # `/loopguard off` (chat-only): record that the guard WOULD
                # have fired, then keep going. Never reached on a YAML config.
                emit(
                    "stuck_loop_suppressed",
                    step=step,
                    consecutive_repeat_steps=st.consecutive_repeat_steps,
                )
            elif cr_abort is not None:
                result.final_text = resp.text or ""
                result.aborted = True
                result.abort_reason = cr_abort["abort_reason"]
                break
        else:
            st.consecutive_repeat_steps = 0
            emit(
                "tool_step_done",
                step=step,
                step_had_repeat=False,
                consecutive_repeat_steps=0,
            )
    else:
        result.final_text = resp.text if resp else ""
        result.aborted = True
        result.abort_reason = f"Max steps reached ({role_cfg.max_steps})"

    # forge-hybrid Phase 2 (A) — resolve-time compaction telemetry. Emits the
    # final per-run cumulative state so post-hoc analysis can attribute
    # outcomes to compaction state. Fires regardless of resolve/abort/max_steps
    # — all paths route through this single return.
    if flags.tiered_compact:
        emit(
            "compaction_phase_at_resolve",
            max_phase_reached=st.compaction_max_phase_this_run,
            phase_at_first_write=st.compaction_phase_at_first_write,
            tool_results_dropped_total=st.compaction_tool_results_dropped_total,
            total_tokens_dropped=st.compaction_total_tokens_dropped,
            # Additive 2026-08-24: how many of those fires achieved nothing.
            ineffective_fires=st.compaction_ineffective_fires,
            aborted=result.aborted,
        )

    if st.tool_results_clamped:
        logger.debug("tool results clamped this run: %d", st.tool_results_clamped)

    result.messages = list(st.messages)
    if not result.aborted and result.final_text:
        result.messages.append({"role": "assistant", "content": result.final_text})
    result.wall_s = time.monotonic() - t0
    return result
