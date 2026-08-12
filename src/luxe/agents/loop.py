"""Shared agent loop — tool dispatch, schema validation, telemetry.

Mirrors luxe's agents/base.py run_agent() pattern: chat → parse tool calls →
validate → dispatch → append results → repeat until done or budget exhausted.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from luxe.agents.cohort_priors import load_prior_from_env
from luxe.agents.convergence import (
    _INTENSITY_NEUTRAL,
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
    ProseBurstGuard,
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
    _TRUNCATED_TURN_MESSAGE,
    TruncatedTurnGuard,
    _PROSE_BURST_MAX_STEP,
    _PROSE_BURST_MESSAGE,
    _PROSE_BURST_MIN_DELTA,
    _WRITE_PRESSURE_MAX_TOOLS_BEFORE_FIRE,
    _WRITE_PRESSURE_MESSAGE,
    _WRITE_PRESSURE_MIN_STEP,
    _WRITE_PRESSURE_MIN_TOKENS,
    _WRITE_PRESSURE_MIN_TOOLS,
    _v1105_synthesis_looping_signature,
)
from luxe.agents.flags import RunFlags
from luxe.backend import Backend, ChatResponse, ToolCallResponse
from luxe.config import RoleConfig
from luxe.context import (
    TieredCompact,
    calibrated_ctx_limit,
    calibration_ratio,
    context_pressure,
    elide_old_tool_results,
    estimate_messages_tokens,
)
from luxe.run_state import append_event
from luxe.spec import Spec
from luxe.spec_validator import validate as spec_validate
from luxe.tools.base import ToolCache, ToolDef, ToolCall, ToolFn, dispatch_tool, validate_args

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
    wall_s: float = 0.0
    peak_context_pressure: float = 0.0
    final_context_pressure: float = 0.0  # last per-step pressure (matches token-progress)


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

# forge-hybrid Phase 3 (B1) — respond terminal tool watchdog constants.
# Default OFF; the loop only intercepts respond calls when
# LUXE_RESPOND_TERMINAL=1 (and the tool is in the surface only under the
# same gate, set by single.py). See src/luxe/tools/respond.py for the
# tool surface and src/luxe/tools/tools.sdd for the contract.
#
# Minimum step before respond is allowed without intervention when no
# write has occurred. Steps below this with writes_seen==0 trip the
# "premature respond" watchdog; steps at or above it trip the
# "no_writes_late" watchdog (soft give-up). Calibrated at 4 to match
# _EARLY_BAIL_MIN_STEP — the same trajectory shape early_bail catches
# is the canonical premature-summarize failure mode for respond.
_RESPOND_MIN_STEP = 4

_RESPOND_PREMATURE_NUDGE = (
    "Mid-loop notice: you called `respond` after only {step} steps "
    "without writing or editing any file. The deliverable for this "
    "task is a concrete change, not a summary. Continue with "
    "`read_file`/`grep` to locate the issue, then `edit_file`/"
    "`write_file`, then call `respond`."
)

_RESPOND_NO_WRITES_LATE_NUDGE = (
    "Mid-loop notice: you've spent {step} steps gathering information "
    "without writing any file, and now you're calling `respond`. If the "
    "existing code is correct and no change is needed, state that "
    "explicitly and call `respond` again. Otherwise, write or edit the "
    "relevant file first."
)

_RESPOND_PASSIVE_SURRENDER_NUDGE = (
    "Mid-loop notice: you wrote a file in step {last_write_step} and "
    "immediately called `respond` without verifying. Use "
    "`read_file`/`grep`/`bash` to confirm the change is correct, then "
    "call `respond`."
)

_RESPOND_COMPACTION_PHANTOM_NUDGE = (
    "Mid-loop notice: context compaction has dropped tool_result content "
    "from earlier in this trajectory, but you have not yet written any "
    "file. Calling `respond` now would summarize from a compacted view. "
    "Use `read_file`/`grep` to re-verify the file you intend to change, "
    "then `edit_file`/`write_file`, then call `respond`."
)

# Emit a progress line each time cumulative completion tokens crosses a
# multiple of this threshold. Useful for spotting bailout vs full-engagement
# patterns mid-run. Set to 0 to disable. Configurable via env.
import os as _os_for_logging
_TOKEN_LOG_INTERVAL = int(_os_for_logging.environ.get("LUXE_TOKEN_LOG_INTERVAL", "5000"))


def _call_key(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True)}"


def run_agent(
    backend: Backend,
    role_cfg: RoleConfig,
    *,
    system_prompt: str,
    task_prompt: str,
    tool_defs: list[ToolDef],
    tool_fns: dict[str, ToolFn],
    cache: ToolCache | None = None,
    cacheable: set[str] | None = None,
    on_tool_event: OnToolEvent | None = None,
    run_id: str | None = None,
    phase: str = "main",
    spec: Spec | None = None,
    early_bail_message: str | None = None,
    on_token: Callable[[str], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
    on_notice: Callable[[str], None] | None = None,
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
    `_EARLY_BAIL_MESSAGE` for this run. SWE-bench adapter passes a
    variant without the abstain branch ("explicitly state the existing
    code is correct"), which was the source of 3 wrong→empty regressions
    in v1.7's B.5. maintain_suite uses the default (abstain is sometimes
    a legitimate outcome there). Pass None to use the default.

    `on_notice` (2026-08-11) receives one-line, human-facing statements about
    the loop acting on its own — today, the truncated-turn retry. It exists
    because that mechanism can spend several minutes of a chat turn (a full
    capped generation per retry) while the UI shows nothing but a spinner:
    the only record was `events.jsonl`, which nobody reads mid-turn. Display
    only. It never influences control flow, is never consulted for a decision,
    and defaults to None, so the benchmark/maintain path is unchanged.
    """

    result = AgentResult()
    t0 = time.monotonic()

    def _notice(text: str) -> None:
        """Display-only; a broken front-end callback must not kill the run."""
        if on_notice is None:
            return
        try:
            on_notice(text)
        except Exception:
            logger.debug("on_notice raised", exc_info=True)
    # Every LUXE_* switch this run obeys, read once, here (agents/flags.py).
    # Same variables, same defaults, same malformed-value fallbacks as the
    # sixteen scattered os.environ.get() calls this replaced; each is still
    # assigned to the local name the body below uses.
    flags = RunFlags.from_env()
    # v1.10.1 — log_calls default-on when run_id is set. Earlier policy
    # was opt-in via LUXE_LOG_TOOL_CALLS=1, which silently degraded the
    # v1.10 production taxonomy (intervention fires + tool_calls invisible)
    # for any run that didn't have the env exported. Default-on closes
    # the footgun the v1.10 audit caught. Opt out via LUXE_SUPPRESS_TOOL_LOG=1
    # (ablation parity for legacy callers).
    log_calls = bool(run_id) and not flags.suppress_tool_log

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_prompt},
    ]

    openai_tools = [td.to_openai() for td in tool_defs] if tool_defs else None
    tool_def_map = {td.name: td for td in tool_defs}
    known_names = set(tool_def_map.keys())

    seen_calls: set[str] = set()
    consecutive_repeat_steps = 0
    next_token_log_threshold = _TOKEN_LOG_INTERVAL  # 0 = disabled
    write_pressure_enabled = flags.write_pressure
    write_pressure_fired = False
    early_bail_enabled = flags.early_bail
    early_bail_fired = False
    early_bail_step: int | None = None  # v1.9: needed by post-bail rescue gate
    # Refined port (2026-05-26 edit-quality investigation, project_track0_*) —
    # when LUXE_EARLY_BAIL_COMMIT_ONLY=1 AND mode=soft_anchor, suppress the
    # mid/low-convergence variants (breadth_probe, soft_anchor) and let only
    # commit_imperative (score >= _CONVERGENCE_HIGH_THRESHOLD) fire. The Phase-1
    # diagnostic showed soft_anchor + breadth_probe correlate with degraded edit
    # quality on the 3 forge-only wins at n=75; the +10.67pp --no-early-bail
    # ablation cleared resolves but failed the wrong-target watchdog (3-4 new
    # wrong_target migrations from baseline empty_patch). This flag tests whether
    # keeping the high-convergence imperative recovers the watchdog cleanly.
    # Default OFF (byte-identical with baseline).
    early_bail_commit_only = flags.early_bail_commit_only
    prose_burst_enabled = flags.prose_burst
    prose_burst_fired = False
    # v1.9 — LUXE_ACTION_DENSITY_GATE (staged escalation second-stage rescue
    # after early_bail stalls). See _ACTION_DENSITY_GATE_* constants above.
    action_density_gate_enabled = flags.action_density_gate
    action_density_gate_fired = False
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
    convergence_gate_enabled = flags.convergence_gate
    post_write_idle_repeats = flags.post_write_idle_repeats
    truncated_turn_retry_enabled = flags.truncated_turn_retry
    truncated_turn_max_retries = flags.truncated_turn_max_retries
    truncated_turn_retries_used = 0
    # Server-truth context calibration (2026-08-11). `estimate_tokens` is
    # chars//4 and reads ~1.9x low on code + JSON tool payloads, so every
    # compaction threshold fired at roughly twice the context it named. Each
    # response's `usage.prompt_tokens` corrects the next step's reading.
    # 1.0 = uncalibrated, which is both the step-1 state and the ablation.
    ctx_server_truth_enabled = flags.ctx_server_truth
    ctx_calibration = 1.0
    # forge-hybrid Phase 2 (A) — TieredCompact context compaction. DEFAULT-ON
    # as of 2026-05-28 (cycle closeout commit). The n=75 rep-1+rep-2
    # validation at phase_thresholds=(0.50, 0.85, 0.95) confirmed: resolve
    # rate equivalent to no-compaction baseline (within substrate noise band
    # ±2.8 at n=75), 42-56% wall savings, 2 protected wrong_target instances
    # healed (matplotlib-25775, pylint-6528). Set LUXE_TIERED_COMPACT=0 to
    # disable for ablation; any other value (or unset) keeps it ON. Default
    # phase_thresholds come from TieredCompact._DEFAULT_PHASE_THRESHOLDS.
    tiered_compact_enabled = flags.tiered_compact
    # LUXE_TIERED_COMPACT_THRESHOLD overrides the default trigger (0.75) for
    # stress-testing; LUXE_TIERED_COMPACT_PHASE_THRESHOLDS ("p1,p2,p3") beats
    # it when valid, mirroring forge's TieredCompact.phase_thresholds. Both
    # parse (and silently fall back) in RunFlags.from_env.
    _tc_threshold = flags.tiered_compact_threshold
    _tc_phase_thresholds = flags.tiered_compact_phase_thresholds
    _tiered_compactor: TieredCompact | None = (
        TieredCompact(
            compact_threshold=_tc_threshold,
            phase_thresholds=_tc_phase_thresholds,
        ) if tiered_compact_enabled else None
    )
    compaction_tool_results_dropped_total = 0
    compaction_total_tokens_dropped = 0
    compaction_max_phase_this_run = 0
    compaction_phase_at_first_write: int | None = None

    # forge-hybrid Phase 3 (B1) — respond terminal tool. Default OFF
    # (byte-identical baseline preserved). When LUXE_RESPOND_TERMINAL=1,
    # the model can call respond(message=...) to exit the loop, gated by
    # 4 watchdogs (compaction-phantom, early-respond, no-writes-late,
    # passive-surrender). See src/luxe/tools/respond.py + tools.sdd.
    respond_terminal_enabled = flags.respond_terminal
    first_write_step: int | None = None
    last_write_step: int | None = None
    respond_terminated = False

    # v1.11 Phase 1 — adaptive policy substrate. Computation + observability
    # ONLY in Phase 1; modulation does NOT yet influence intervention
    # dispatch (deferred to Phase 3a so any behavior change is gated under
    # archetype-probe testing). Disable-equivalence invariant: when
    # LUXE_ADAPTIVE_POLICY=0 (or unset), zero adaptive_state events are
    # emitted and zero new state is computed — v1.10.5 byte-identical.
    adaptive_policy_enabled = flags.adaptive_policy
    # Per-signal ablation toggles (default ON when adaptive_policy_enabled).
    adaptive_no_write_enabled = flags.adaptive_no_write
    adaptive_score_trend_enabled = flags.adaptive_score_trend
    # v1.11 Phase 3a — slew-rate limit; agents.sdd-pinned default 0.3.
    # Bounds per-step intensity-modifier change. Override for ablation.
    adaptive_max_delta = flags.adaptive_max_delta
    # Modulation state per intervention kind; starts neutral (1.0 = no change).
    # Updated each step (slew-rate-limited) when adaptive_policy_enabled.
    # v1.11 status: ALL THREE modulations are computed + emitted for
    # observability but NONE acts on dispatch. The Phase B soft_anchor collapse
    # promotion was reverted (net-negative at n=75 — premature-commitment tier
    # demotion). write_pressure/early_bail bias was retired in Phase A
    # (no_write non-selective). soft_anchor bias is still computed (shows where a
    # future, more-specific stall signal would fire) but no consumer remains.
    intervention_modulation: dict[str, float] = {
        "write_pressure": _INTENSITY_NEUTRAL,
        "early_bail": _INTENSITY_NEUTRAL,
        "soft_anchor": _INTENSITY_NEUTRAL,
    }
    # Bounded per-step score log; owned by loop.py per the agents.sdd
    # composition boundary (convergence.py is the sole consumer, never
    # mutates it).
    score_log: list[float] = []
    # v1.11 Phase 2 — cross-cycle prior (log-only this cycle). Read once
    # at run start. Priors do NOT influence intervention intensity in
    # v1.11 per agents.sdd ("priors-log-only" invariant); deferred to
    # v1.11.1+. Loader is null-safe — missing/corrupt input returns None.
    cohort_prior = load_prior_from_env()
    if cohort_prior is not None and log_calls:
        append_event(
            run_id, "prior_loaded",
            phase=phase,
            instance_id=cohort_prior.get("instance_id"),
            verdict=cohort_prior.get("verdict"),
            tiers_a=cohort_prior.get("tiers_a"),
            tiers_b=cohort_prior.get("tiers_b"),
            rank_delta=cohort_prior.get("rank_delta"),
        )
    # v1.10.4 — band-response policy for the score<LOW suppression branch.
    # "silent"               = v1.10.3 behavior (blanket silent suppression)
    # "breadth_probe_hybrid" = v1.10.4 default (fire breadth_probe on the
    #                         first suppression AND on the Nth escalation
    #                         suppression where N=_BREADTH_PROBE_ESCALATION_COUNT;
    #                         silent on intervening suppressions)
    # The hybrid restores the v1.10.2-style first-event nudge that
    # sphinx-10435 needs, while keeping suppression silent enough on
    # subsequent events to avoid the v1.10.1 wasted-runway shape that
    # broke matplotlib-14623.
    _band_response = flags.early_bail_band_response
    suppression_count_in_trajectory = 0
    breadth_probe_fire_count = 0
    # v1.9 — convergence proxy. Track read_file call signatures so the gate
    # can suppress itself when the model has revisited the same file (strong
    # trajectories rerun reads ~3× more often than empties per the v18
    # distribution; that's a "found my target" signal).
    read_keys_seen: set[str] = set()
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
    intervention_kinds_fired: set[str] = set()
    # v1.10 — convergence-score telemetry. tool_history is a bounded list of
    # (name, path) entries for the convergence score (see
    # luxe.agents.convergence). post-intervention behavior signals capture
    # whether the model engaged after a fire (lag-to-write + sustained-write
    # signals). All observability — no gating on these yet (Item 2 wires
    # gating; Item 1 establishes the substrate).
    TOOL_HISTORY_MAX = 20
    tool_history: list[dict[str, Any]] = []
    first_write_step_after_intervention: int | None = None
    post_intervention_consecutive_writes = 0
    post_intervention_write_burst_max = 0
    prev_completion_tokens = 0
    prev_tool_calls_total_at_sample = 0  # v1.9 — for next_action_was_tool_call
    writes_seen = 0
    post_write_idle_tools = 0

    # SpecDD Lever 1 mid-loop state (v1.7). actual_tool_calls accumulates
    # (name, args) for every dispatched call so the spec validator sees the
    # same shape the BFCL adapter does. spec_violations_reprompted tracks
    # which requirement ids have already triggered a reprompt so each fires
    # at most once.
    spec_has_zero_calls = (
        spec is not None
        and any(r.kind == "expects_zero_calls" for r in spec.requirements)
    )
    if spec_has_zero_calls:
        # Suppression: the four tool-eagerness amplifiers (write_pressure,
        # early_bail, prose_burst, action_density_gate) would push the
        # model toward action exactly when the correct outcome is to
        # decline. Disable all when the spec contains a zero-call
        # expectation. v1.10 convergence_gate has no effect when the
        # gated interventions are themselves disabled, but we mirror the
        # off-switch for clarity.
        write_pressure_enabled = False
        early_bail_enabled = False
        prose_burst_enabled = False
        action_density_gate_enabled = False
        convergence_gate_enabled = False
    actual_tool_calls: list[tuple[str, dict[str, Any]]] = []
    spec_violations_reprompted: set[str] = set()
    # The previous iteration's response. Bound here (not just inside the loop)
    # because the pre-step clean-exit guards below read it before this step's
    # backend.chat runs; on step 0 there is nothing to read and `""` is right.
    # Was a latent NameError + a `'resp' in dir()` probe (ruff F821, 2026-07-29).
    resp: ChatResponse | None = None

    for step in range(role_cfg.max_steps):
        result.steps = step + 1

        # The window every pressure consumer divides by this step. From step 2
        # on it is `num_ctx` shrunk by however far the chars/4 estimate ran
        # under the server's own `usage.prompt_tokens` last call — see
        # `calibrated_ctx_limit`. Step 1 has nothing to calibrate against and
        # uses `num_ctx` raw, which is the historical behaviour.
        effective_ctx = calibrated_ctx_limit(role_cfg.num_ctx, ctx_calibration)

        pressure = context_pressure(messages, effective_ctx)
        result.peak_context_pressure = max(result.peak_context_pressure, pressure)
        result.final_context_pressure = pressure  # instantaneous; matches token-progress
        # Per-step ctx forensics. `est` is the raw chars/4 reading, `cal` the
        # correction applied; when cal != 1.0 the reported pressure is
        # server-calibrated and should track chat's finalize_turn number rather
        # than sitting at half of it. debug.log-only; see logger note at top.
        logger.debug("step=%d ctx_pressure=%.1f%% (est=%.1f%% cal=%.2fx) "
                     "num_ctx=%d effective_ctx=%d msgs=%d",
                     step + 1, pressure * 100,
                     context_pressure(messages, role_cfg.num_ctx) * 100,
                     ctx_calibration, role_cfg.num_ctx, effective_ctx,
                     len(messages))
        if on_progress is not None:
            on_progress(pressure)  # chat-only live ctx% (one source of truth, C2)

        # v1.10 — compute convergence score ONCE per step at the top of the
        # iteration. Used by both early_bail and action_density_gate
        # predicates AND emitted on the action_density_sample observability
        # event. Pure function over the bounded tool_history; cheap to
        # evaluate every step. The score is the v1.10 replacement for the
        # v1.9 binary `same_file_read_twice_step` skip — see
        # luxe.agents.convergence module docstring for the design rationale.
        convergence_score = compute_convergence_score(tool_history)
        # v1.11 Phase 1 — append to score_log + compute adaptive state.
        # Guarded by LUXE_ADAPTIVE_POLICY to preserve disable-equivalence.
        # Phase 1 behavior: emit observability event only; state does NOT
        # influence intervention dispatch this phase.
        if adaptive_policy_enabled:
            score_log.append(convergence_score)
            # Bounded growth: cap at 64 entries (covers max_steps for current
            # configs with headroom). Drop oldest when over.
            if len(score_log) > 64:
                score_log[:] = score_log[-64:]
            adaptive_state = compute_within_run_state(
                score_log, tool_history, step,
                no_write_enabled=adaptive_no_write_enabled,
                score_trend_enabled=adaptive_score_trend_enabled,
            )
            # v1.11 Phase 3a — compute bias → target modulation → slew-rate-limited update.
            bias = compute_intervention_bias(adaptive_state)
            for kind, prev_mod in list(intervention_modulation.items()):
                target = bias_to_modulation(bias.get(kind, 0.0))
                intervention_modulation[kind] = apply_slew_rate(
                    prev_mod, target, max_delta=adaptive_max_delta,
                )
            if log_calls:
                append_event(
                    run_id, "adaptive_state",
                    phase=phase, step=step,
                    consecutive_no_write=adaptive_state.consecutive_no_write,
                    score_trend=adaptive_state.score_trend,
                    score_log_len=adaptive_state.score_log_len,
                    convergence_score=convergence_score,
                    modulation_write_pressure=intervention_modulation["write_pressure"],
                    modulation_early_bail=intervention_modulation["early_bail"],
                    modulation_soft_anchor=intervention_modulation["soft_anchor"],
                )

        # Mid-loop write-pressure injection (Mode B fix). Fires once per
        # run when the agent has done substantial reading + generation
        # without writing. Targets the prose-mode trap where the model
        # declares "comprehensive picture" prematurely and hallucinates
        # the deliverable into chat instead of committing it. The
        # synthetic user message interrupts the read-loop and forces a
        # write decision before further tool calls accumulate.
        # Write-pressure guard. Decision logic extracted to
        # luxe.agents.guardrails.WritePressureGuard (forge-hybrid Phase 1).
        # Loop owns state mutation (the *_fired flag, intervention tracking
        # vars, event emission) so behavior is unchanged on the wire.
        wp_decision = WritePressureGuard.check(
            write_pressure_enabled=write_pressure_enabled,
            write_pressure_fired=write_pressure_fired,
            writes_seen=writes_seen,
            step=step,
            tool_calls_total=result.tool_calls_total,
            completion_tokens=result.completion_tokens,
            adaptive_policy_enabled=adaptive_policy_enabled,
            intervention_modulation_write_pressure=intervention_modulation["write_pressure"],
        )
        if wp_decision is not None:
            messages.append({
                "role": "user",
                "content": wp_decision.message,
                "_luxe_nudge": True,
                "_luxe_nudge_type": WritePressureGuard.nudge_type,
            })
            write_pressure_fired = True
            last_intervention_step = step
            last_intervention_kind = "write_pressure"
            intervention_kinds_fired.add("write_pressure")
            if log_calls:
                append_event(
                    run_id, "write_pressure_fired",
                    phase=phase, step=step,
                    tool_calls_total=result.tool_calls_total,
                    completion_tokens=result.completion_tokens,
                )

        # Early-bail intervention (v1.7 priority #1). Same checkpoint as
        # write_pressure but fires earlier — at step 4 with 4+ non-write
        # tool calls and zero writes. Trace-derived thresholds; see the
        # block comment on _EARLY_BAIL_MIN_STEP above for the empirical
        # basis. The message gives the model a binary recovery gradient:
        # edit OR explicitly decline-with-justification. Mutually
        # compatible with WRITE_PRESSURE — both can fire in the same run
        # since they target different trajectory shapes.
        eb_outcome = EarlyBailGuard.evaluate(
            early_bail_enabled=early_bail_enabled,
            early_bail_fired=early_bail_fired,
            writes_seen=writes_seen,
            step=step,
            tool_calls_total=result.tool_calls_total,
            early_bail_message=early_bail_message,
            early_bail_commit_only=early_bail_commit_only,
            convergence_gate_enabled=convergence_gate_enabled,
            convergence_score=convergence_score,
            band_response=_band_response,
            suppression_count_in_trajectory=suppression_count_in_trajectory,
            tool_history=tool_history,
            recent_path_diversity=recent_path_diversity(tool_history),
            score_log=score_log,
            early_bail_step=early_bail_step,
        )
        if eb_outcome is not None:
            # Apply state mutations the loop owns. The guard tells us how
            # much to change suppression_count / breadth_probe_fire_count,
            # whether to set early_bail_fired, and which intervention-kind
            # to record on the trackers.
            suppression_count_in_trajectory += eb_outcome.suppression_count_delta
            breadth_probe_fire_count += eb_outcome.breadth_probe_fire_delta
            if eb_outcome.sets_early_bail_fired:
                early_bail_fired = True
                early_bail_step = step
            if eb_outcome.decision is not None:
                msg_dict: dict[str, Any] = {
                    "role": "user",
                    "content": eb_outcome.decision.message,
                }
                if eb_outcome.nudge_type is not None:
                    msg_dict["_luxe_nudge"] = True
                    msg_dict["_luxe_nudge_type"] = eb_outcome.nudge_type
                messages.append(msg_dict)
            if eb_outcome.last_intervention_kind is not None:
                last_intervention_step = step
                last_intervention_kind = eb_outcome.last_intervention_kind
                intervention_kinds_fired.add(eb_outcome.last_intervention_kind)
            if log_calls:
                if eb_outcome.suppress_event is not None:
                    ev_name, ev_payload = eb_outcome.suppress_event
                    # Loop owns the completion_tokens counter; fill it for
                    # events that carry it (commit_only suppression).
                    payload = {
                        k: (result.completion_tokens if k == "completion_tokens" and v is None else v)
                        for k, v in ev_payload.items()
                    }
                    append_event(run_id, ev_name, phase=phase, step=step, **payload)
                if eb_outcome.fire_event is not None:
                    ev_name, ev_payload = eb_outcome.fire_event
                    payload = {
                        k: (result.completion_tokens if k == "completion_tokens" and v is None else v)
                        for k, v in ev_payload.items()
                    }
                    append_event(run_id, ev_name, phase=phase, step=step, **payload)

        # Per-step deltas (v1.8 Track 1 plumbing). Used by prose_burst,
        # action_density_gate, and the action_density_sample observability
        # event. completion_delta_last_step is the SIZE of the previous
        # step's response — we evaluate at the start of step N to catch a
        # step N-1 burst, leaving budget for the intervention to land.
        completion_delta_last_step = result.completion_tokens - prev_completion_tokens
        action_density = (
            (result.tool_calls_total / max(1, result.completion_tokens))
            if result.completion_tokens > 0 else 0.0
        )

        # v1.9 — LUXE_ACTION_DENSITY_GATE. Staged-escalation predicate that
        # fires once per run in one of two modes:
        #   - standalone:        early_bail never fired; gate stands alone
        #   - post_bail_rescue:  early_bail fired ≥MIN_TURNS_AFTER_BAIL
        #                        turns ago AND no writes since
        # Convergence proxy (same_file_read_twice on/before this step)
        # suppresses the gate — strong trajectories converge by re-reading
        # the same target. Thresholds derived from
        # scripts/mine_action_density.py over v17 + v18 SWE-bench n=75;
        # see acceptance/v19_mining/THRESHOLD_DECISION.md.
        # v1.10 — convergence-score suppression replaces the v1.9 binary
        # same_file_read_twice skip. When the gate is enabled AND the
        # model has converged (score >= HIGH), suppress the gate — the
        # rescue would interrupt a trajectory that's converging on its
        # own. Keep the v1.9 same_file_read_twice_step as a fallback skip
        # condition when the convergence gate is OFF (preserves v1.9
        # ablation semantics).
        v110_suppress = (
            convergence_gate_enabled
            and convergence_score >= _CONVERGENCE_HIGH_THRESHOLD
        )
        v19_suppress = (
            not convergence_gate_enabled
            and same_file_read_twice_step is not None
            and same_file_read_twice_step <= step
        )
        adg_decision = ActionDensityGateGuard.check(
            action_density_gate_enabled=action_density_gate_enabled,
            action_density_gate_fired=action_density_gate_fired,
            writes_seen=writes_seen,
            step=step,
            completion_tokens=result.completion_tokens,
            tool_calls_total=result.tool_calls_total,
            v110_suppress=v110_suppress,
            v19_suppress=v19_suppress,
            early_bail_step=early_bail_step,
        )
        if adg_decision is not None:
            messages.append({
                "role": "user",
                "content": adg_decision.message,
                "_luxe_nudge": True,
                "_luxe_nudge_type": ActionDensityGateGuard.nudge_type,
            })
            action_density_gate_fired = True
            last_intervention_step = step
            last_intervention_kind = "action_density_gate"
            intervention_kinds_fired.add("action_density_gate")
            if log_calls:
                append_event(
                    run_id, "action_density_gate_fired",
                    phase=phase, step=step,
                    fire_mode=adg_decision.metadata["fire_mode"],
                    turns_since_bail=adg_decision.metadata["turns_since_bail"],
                    tool_calls_total=result.tool_calls_total,
                    completion_tokens=result.completion_tokens,
                    action_density=action_density,
                    same_file_read_twice_step=same_file_read_twice_step,
                    convergence_score=convergence_score,
                )
        elif (action_density_gate_enabled and not action_density_gate_fired
              and v110_suppress and log_calls):
            # Observability — record the v1.10 suppression once when it
            # would otherwise have fired, so post-hoc analysis can tell
            # convergence-suppression from threshold-miss.
            if (writes_seen == 0
                    and step >= _ACTION_DENSITY_GATE_MIN_STEP
                    and result.completion_tokens >= _ACTION_DENSITY_GATE_MIN_TOKENS
                    and result.tool_calls_total <= _ACTION_DENSITY_GATE_MAX_TOOLS):
                append_event(
                    run_id, "action_density_gate_suppressed_converged",
                    phase=phase, step=step,
                    convergence_score=convergence_score,
                    threshold=_CONVERGENCE_HIGH_THRESHOLD,
                )

        # v1.10.2 — post-exploratory escalation REMOVED before ship.
        # The probe revealed matplotlib-14623 (W3 founding recovery)
        # and pylint-6528 (W3 collateral) have CONTRADICTORY needs at
        # the same convergence-score band: pylint-6528 NEEDED escalation
        # pressure to commit (step 8 escalation → step 8 edit_file);
        # matplotlib-14623 was on a successful late-commit trajectory
        # (step 14 write in v1.10.1) that the escalation cascade
        # interrupted, regressing it to habituation_exit at step 20
        # with 0 writes. Single-mechanism escalation can't satisfy both;
        # v1.10.3 needs a discriminator at fire-time. v1.10.2 ships as
        # observability-only (no model-behavior change beyond the
        # diversity gate's minimal-trajectory fallback).

        # v1.8 Track 1 — prose-burst detector. Composite invariant fires at
        # most once per run; on second consecutive burst (intervention
        # produced no response change), exit cleanly.
        prose_burst_now = (
            step <= _PROSE_BURST_MAX_STEP
            and result.tool_calls_total == 0
            and writes_seen == 0
            and completion_delta_last_step >= _PROSE_BURST_MIN_DELTA
        )
        pb_decision = ProseBurstGuard.check(
            prose_burst_enabled=prose_burst_enabled,
            prose_burst_fired=prose_burst_fired,
            step=step,
            tool_calls_total=result.tool_calls_total,
            writes_seen=writes_seen,
            completion_delta_last_step=completion_delta_last_step,
        )
        if pb_decision is not None:
            messages.append({
                "role": "user",
                "content": pb_decision.message,
                "_luxe_nudge": True,
                "_luxe_nudge_type": ProseBurstGuard.nudge_type,
            })
            prose_burst_fired = True
            last_intervention_step = step
            last_intervention_kind = "prose_burst"
            intervention_kinds_fired.add("prose_burst")
            if log_calls:
                append_event(
                    run_id, "prose_burst_fired",
                    phase=phase, step=step,
                    completion_delta=completion_delta_last_step,
                    completion_tokens=result.completion_tokens,
                    action_density=action_density,
                )
        elif prose_burst_enabled and prose_burst_fired and prose_burst_now:
            # Anti-oscillation: intervention fired last step; this step is
            # ALSO a prose burst with no action. Trajectory is non-steerable.
            # Clean exit (not aborted) to preserve trace + evaluation
            # semantics; the model has demonstrated unresponsiveness to the
            # control layer. `resp` is the prior iteration's response (the
            # second burst), still bound in local scope here.
            result.final_text = (resp.text if resp else "") or ""
            if log_calls:
                append_event(
                    run_id, "prose_burst_clean_exit",
                    phase=phase, step=step,
                    completion_delta=completion_delta_last_step,
                    completion_tokens=result.completion_tokens,
                )
            break

        # v1.10.1 — habituation clean-exit. When ≥3 distinct interventions
        # have fired this run AND the model has produced ZERO post-intervention
        # writes AND step ≥ _HABITUATION_EXIT_MIN_STEP, the trajectory is
        # intervention-resistant. Burning the remaining max_steps budget
        # yields no further information. Exit cleanly to preserve trace +
        # evaluation semantics (mirrors prose_burst_clean_exit and
        # post_write_idle_exit shapes). Founding instance: sympy-13031 fired
        # all three distinct interventions by step 15, zero writes through
        # max_steps. `resp` is from the prior iteration's backend.chat call.
        hab_exit = HabituationExitGuard.should_exit(
            intervention_kinds_fired=intervention_kinds_fired,
            first_write_step_after_intervention=first_write_step_after_intervention,
            step=step,
            last_intervention_step=last_intervention_step,
            tool_calls_total=result.tool_calls_total,
            completion_tokens=result.completion_tokens,
        )
        if hab_exit is not None:
            result.final_text = (resp.text if resp else "") or ""
            if log_calls:
                append_event(
                    run_id, "habituation_exit",
                    phase=phase, step=step,
                    **hab_exit,
                )
            break

        # Observability: emit action_density per step regardless of gating.
        # Becomes the dataset for adaptive threshold tuning. Cheap.
        if log_calls and step > 0:
            # v1.9 habituation telemetry: when an intervention has fired,
            # report (a) how many steps since it fired, (b) which one, and
            # (c) whether the immediately-following step produced any tool
            # call. Lets us post-hoc measure whether text-level interventions
            # remain causally active or accumulate as ignorable background.
            habituation: dict[str, Any] = {}
            if last_intervention_step is not None and step > last_intervention_step:
                step_had_call = result.tool_calls_total > prev_tool_calls_total_at_sample
                habituation = {
                    "since_intervention_step": step - last_intervention_step,
                    "since_intervention_kind": last_intervention_kind,
                    "next_action_was_tool_call": step_had_call,
                    # v1.10 — post-intervention behavior signals. None
                    # until the first post-intervention write fires; once
                    # it does, time_to_first_write_after_intervention is
                    # fixed for the rest of the run. write_burst_persistence
                    # is the running max consecutive post-intervention
                    # writes — captures "stuck on cleanup" vs "real
                    # engagement" once the model commits.
                    "time_to_first_write_after_intervention":
                        first_write_step_after_intervention,
                    "write_burst_persistence": post_intervention_write_burst_max,
                }
            # v1.10 — convergence_score is already computed at top of
            # step (used by early_bail + action_density_gate predicates);
            # just emit it on the sample event for observability.
            append_event(
                run_id, "action_density_sample",
                phase=phase, step=step,
                completion_delta=completion_delta_last_step,
                action_density=action_density,
                writes_seen=writes_seen,
                tool_calls_total=result.tool_calls_total,
                convergence_score=convergence_score,
                **habituation,
            )
        prev_tool_calls_total_at_sample = result.tool_calls_total
        # Capture cumulative tokens BEFORE this step's backend.chat so the
        # next iteration's delta correctly measures THIS step's response.
        prev_completion_tokens = result.completion_tokens

        # SpecDD Lever 1 mid-loop reprompt gate (v1.7). Fires expects_zero_calls
        # reprompts here — the predicate's violation is immediate (any tool
        # call is a violation), so the reprompt lands at the start of the
        # next step after the offending call. min_tool_calls reprompts fire
        # at loop-break, not here, because their natural fire-point is when
        # the model is about to terminate without enough calls.
        if spec is not None and actual_tool_calls:
            vr = spec_validate(spec, "", "", tool_calls=actual_tool_calls)
            for rr in vr.unsatisfied:
                if rr.requirement.id in spec_violations_reprompted:
                    continue
                if rr.requirement.kind != "expects_zero_calls":
                    continue
                messages.append({"role": "user", "content": rr.detail})
                spec_violations_reprompted.add(rr.requirement.id)
                if log_calls:
                    append_event(
                        run_id, "spec_reprompt_fired",
                        phase=phase, step=step,
                        requirement_id=rr.requirement.id,
                        requirement_kind=rr.requirement.kind,
                    )

        if tiered_compact_enabled and _tiered_compactor is not None:
            cr = _tiered_compactor.compact(messages, effective_ctx)
            messages = cr.messages
            if cr.phase_reached > 0:
                compaction_tool_results_dropped_total += cr.tool_results_dropped
                compaction_total_tokens_dropped += (cr.tokens_before - cr.tokens_after)
                if cr.phase_reached > compaction_max_phase_this_run:
                    compaction_max_phase_this_run = cr.phase_reached
                if log_calls:
                    append_event(
                        run_id, "compaction_phase_reached",
                        phase=phase, step=step,
                        phase_reached=cr.phase_reached,
                        tokens_before=cr.tokens_before,
                        tokens_after=cr.tokens_after,
                        tool_results_dropped=cr.tool_results_dropped,
                    )
        else:
            messages = elide_old_tool_results(messages, effective_ctx)

        # What we are about to send, measured the same way the calibration
        # divides it — AFTER compaction, so the ratio describes the request the
        # server actually answers.
        est_sent = estimate_messages_tokens(messages)

        try:
            resp = backend.chat(
                messages,
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

        result.prompt_tokens += resp.timing.prompt_tokens
        result.completion_tokens += resp.timing.completion_tokens
        if resp.timing.prompt_tokens:
            result.last_prompt_tokens = resp.timing.prompt_tokens

        # Recalibrate from the response we just got. Re-measured every step
        # rather than latched once: the mix shifts as tool payloads accumulate,
        # and a run that starts on prose and ends on JSON should not keep the
        # first step's ratio. `calibration_ratio` clamps and degrades to 1.0 on
        # a missing/zero usage report.
        if ctx_server_truth_enabled:
            new_cal = calibration_ratio(resp.timing.prompt_tokens, est_sent)
            if new_cal != ctx_calibration:
                logger.debug("ctx calibration %.2fx -> %.2fx "
                             "(server=%d est=%d)", ctx_calibration, new_cal,
                             resp.timing.prompt_tokens, est_sent)
            ctx_calibration = new_cal

        # Token-interval progress logging — fires when cumulative completion
        # tokens crosses each LUXE_TOKEN_LOG_INTERVAL multiple. Lets us see
        # whether a model is steadily generating with tool calls (engaged)
        # vs bursting prose without tools (bailing).
        if (next_token_log_threshold > 0
                and result.completion_tokens >= next_token_log_threshold):
            print(
                f"    [token-progress] step={step+1} "
                f"completion_tokens={result.completion_tokens} "
                f"prompt_tokens={result.prompt_tokens} "
                f"tool_calls={result.tool_calls_total} "
                f"ctx_pressure={pressure:.0%}",
                flush=True,
            )
            while next_token_log_threshold <= result.completion_tokens:
                next_token_log_threshold += _TOKEN_LOG_INTERVAL

        tool_calls = resp.tool_calls
        if not tool_calls and resp.text and tool_defs:
            text_drops: list[str] = []
            tool_calls = _parse_text_tool_calls(resp.text, known_names,
                                                drops=text_drops)
            if text_drops:
                logger.warning(
                    "text-fallback dropped unknown tool call(s): %s",
                    [d[:80] for d in text_drops])
                if log_calls:
                    append_event(
                        run_id, "textfallback_drop",
                        phase=phase, step=step,
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
                truncated_turn_retry_enabled=truncated_turn_retry_enabled,
                finish_reason=getattr(resp, "finish_reason", "") or "",
                has_tool_calls=bool(tool_calls),
                retries_used=truncated_turn_retries_used,
                max_retries=truncated_turn_max_retries,
            )
            if tt is not None:
                # Record the cut-off text before the nudge so the transcript
                # shows what the model was mid-way through saying.
                if resp.text:
                    messages.append({"role": "assistant", "content": resp.text})
                messages.append({
                    "role": "user",
                    "content": _TRUNCATED_TURN_MESSAGE,
                    "_luxe_nudge": True,
                    "_luxe_nudge_type": TruncatedTurnGuard.nudge_type,
                })
                truncated_turn_retries_used += 1
                if log_calls:
                    append_event(
                        run_id, "truncated_turn_retry",
                        phase=phase, step=step,
                        retries_used=truncated_turn_retries_used,
                        max_retries=truncated_turn_max_retries,
                        completion_tokens=resp.timing.completion_tokens,
                    )
                logger.debug(
                    "truncated turn retry step=%d retries_used=%d max=%d "
                    "completion_tokens=%d",
                    step, truncated_turn_retries_used,
                    truncated_turn_max_retries, resp.timing.completion_tokens)
                _notice(
                    f"answer cut off at the {resp.timing.completion_tokens:,}-token "
                    f"cap with no tool call — retrying "
                    f"({truncated_turn_retries_used}/{truncated_turn_max_retries}); "
                    f"each retry costs another full generation"
                )
                continue

            # SpecDD Lever 1 min_tool_calls gate: before declaring the run
            # finished, check whether the spec expects more tool calls than
            # the model has emitted. If so, inject a reprompt and continue
            # the loop instead of breaking. Each requirement fires at most
            # once per run, so a stuck model can't ping-pong forever.
            if spec is not None and tool_defs:
                vr = spec_validate(spec, "", "", tool_calls=actual_tool_calls)
                continue_for_spec = False
                for rr in vr.unsatisfied:
                    if rr.requirement.id in spec_violations_reprompted:
                        continue
                    if rr.requirement.kind != "min_tool_calls":
                        continue
                    messages.append({"role": "user", "content": rr.detail})
                    spec_violations_reprompted.add(rr.requirement.id)
                    continue_for_spec = True
                    if log_calls:
                        append_event(
                            run_id, "spec_reprompt_fired",
                            phase=phase, step=step,
                            requirement_id=rr.requirement.id,
                            requirement_kind=rr.requirement.kind,
                        )
                if continue_for_spec:
                    # Replay the assistant's final text so the conversation
                    # history records the would-be exit before the reprompt.
                    if resp.text:
                        messages.append({"role": "assistant", "content": resp.text})
                    continue
            # TELEMETRY ONLY — additive, ungated, never touches control flow or
            # `messages` (agents.sdd "Tool-call telemetry events"). A run that
            # ends here on finish_reason="length" was CUT OFF mid-answer, not
            # finished; without this the records cannot tell the two apart,
            # which is how the strict-flag fixture read as a clean completion
            # for six identical runs.
            if log_calls and (getattr(resp, "finish_reason", "") or "") == "length":
                append_event(
                    run_id, "terminal_turn_truncated",
                    phase=phase, step=step,
                    completion_tokens=resp.timing.completion_tokens,
                    final_text_chars=len(resp.text or ""),
                    retry_enabled=truncated_turn_retry_enabled,
                    retries_used=truncated_turn_retries_used,
                )
            if (getattr(resp, "finish_reason", "") or "") == "length":
                _notice(
                    f"answer cut off at the {resp.timing.completion_tokens:,}-token "
                    f"cap — ending the turn "
                    + (f"({truncated_turn_retries_used} retr"
                       + ("y" if truncated_turn_retries_used == 1 else "ies")
                       + " already used)"
                       if truncated_turn_retries_used
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
            messages.append({"role": "assistant", "content": assistant_text})
            messages.append({"role": "user", "content": (
                "Tool calls are not permitted for this request. The "
                "available tools cannot answer the user's question. "
                "Reply only in prose, briefly explaining why the request "
                "is out of scope."
            )})
            if log_calls:
                append_event(
                    run_id, "spec_predispatch_blocked",
                    phase=phase, step=step,
                    blocked_tool_names=[tc.name for tc in tool_calls],
                    blocked_count=len(tool_calls),
                )
            # Skip the dispatch loop entirely. Tool calls are dropped on
            # the floor — they never enter actual_tool_calls, so the BFCL
            # grader sees zero calls. Continue to next step.
            continue

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": resp.text or ""}
        if resp.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id or f"call_{step}_{i}",
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for i, tc in enumerate(resp.tool_calls)
            ]
        messages.append(assistant_msg)

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

            if tc.name in tool_def_map:
                err = validate_args(tool_def_map[tc.name], tc.arguments)
                if err:
                    result.schema_rejects += 1
                    if log_calls:
                        append_event(
                            run_id, "tool_reject",
                            phase=phase, step=step, name=tc.name,
                            reason="schema", message=str(err)[:300],
                        )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id or f"call_{step}",
                        "name": tc.name,
                        "content": f"Schema error: {err}",
                    })
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
                if key in read_keys_seen and same_file_read_twice_step is None:
                    same_file_read_twice_step = step
                else:
                    read_keys_seen.add(key)
            # Captured BEFORE the dedup branch and before `seen_calls.add(key)`
            # further down, so it means "this exact call already ran this run".
            # Distinct from `step_had_repeat`, which the dedup exemption keeps
            # False for read_file — the very tool that produced the blind spot
            # this feeds (see the post-write idle branch below).
            call_is_repeat = key in seen_calls
            if key in seen_calls and tc.name not in _DEDUP_EXEMPT_TOOLS:
                step_had_repeat = True
                content = (
                    f"You already called {tc.name} with these exact arguments "
                    "and the result was provided above. "
                    "Use a different tool, try different arguments, "
                    "or summarize your findings."
                )
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
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id or f"call_{step}",
                    "name": tc.name,
                    "content": content,
                })
                if on_tool_event:
                    on_tool_event(dup)
                if log_calls:
                    append_event(
                        run_id, "tool_call",
                        phase=phase, step=step, name=tc.name,
                        key_hash=key_hash, duplicate=True, cached=False,
                        bytes_out=0,
                    )
                if writes_seen > 0:
                    post_write_idle_tools += 1
                continue

            # forge-hybrid Phase 3 (B1) — respond terminal tool watchdog
            # intercept. Runs BEFORE dispatch when LUXE_RESPOND_TERMINAL=1
            # and the model calls `respond`. Four gates apply in this
            # priority order; first match wins. Three gates inject a
            # reprompt + continue (do not terminate). The terminal path
            # (no gate fires) sets result.final_text + breaks both the
            # inner tool_calls loop and the outer step loop.
            if respond_terminal_enabled and tc.name == "respond":
                message = str(tc.arguments.get("message", ""))
                # Gate 1: compaction × respond (highest priority).
                if (compaction_max_phase_this_run >= 2
                        and writes_seen == 0):
                    messages.append({
                        "role": "user",
                        "content": _RESPOND_COMPACTION_PHANTOM_NUDGE,
                        "_luxe_nudge": True,
                        "_luxe_nudge_type": "respond_compaction_phantom",
                    })
                    if log_calls:
                        append_event(
                            run_id, "respond_compaction_phantom",
                            phase=phase, step=step,
                            writes_seen=writes_seen,
                            compaction_max_phase=compaction_max_phase_this_run,
                            message_chars=len(message),
                        )
                    continue
                # Gate 2: early-respond watchdog (writes==0, step < MIN).
                if writes_seen == 0 and step < _RESPOND_MIN_STEP:
                    messages.append({
                        "role": "user",
                        "content": f"Mid-loop notice: you called `respond` after only {step} steps without writing or editing any file. The deliverable for this task is a concrete change, not a summary. Continue with `read_file`/`grep` to locate the issue, then `edit_file`/`write_file`, then call `respond`.",
                        "_luxe_nudge": True,
                        "_luxe_nudge_type": "respond_premature",
                    })
                    if log_calls:
                        append_event(
                            run_id, "respond_premature",
                            phase=phase, step=step,
                            writes_seen=writes_seen,
                            message_chars=len(message),
                        )
                    continue
                # Gate 3: no-writes-late (soft give-up).
                if writes_seen == 0 and step >= _RESPOND_MIN_STEP:
                    messages.append({
                        "role": "user",
                        "content": f"Mid-loop notice: you've spent {step} steps gathering information without writing any file, and now you're calling `respond`. If the existing code is correct and no change is needed, state that explicitly and call `respond` again. Otherwise, write or edit the relevant file first.",
                        "_luxe_nudge": True,
                        "_luxe_nudge_type": "respond_no_writes_late",
                    })
                    if log_calls:
                        append_event(
                            run_id, "respond_no_writes_late",
                            phase=phase, step=step,
                            writes_seen=writes_seen,
                            message_chars=len(message),
                        )
                    continue
                # Gate 4: anti-cheap-exit (passive surrender). At this
                # point writes_seen >= 1. PASS iff at least one step
                # elapsed since the most recent write (verification
                # opportunity). FAIL if same-step respond after a write.
                if not (last_write_step is not None and step > last_write_step):
                    messages.append({
                        "role": "user",
                        "content": f"Mid-loop notice: you wrote a file in step {last_write_step} and immediately called `respond` without verifying. Use `read_file`/`grep`/`bash` to confirm the change is correct, then call `respond`.",
                        "_luxe_nudge": True,
                        "_luxe_nudge_type": "respond_passive_surrender",
                    })
                    if log_calls:
                        append_event(
                            run_id, "respond_passive_surrender",
                            phase=phase, step=step,
                            writes_seen=writes_seen,
                            last_write_step=last_write_step,
                            message_chars=len(message),
                        )
                    continue
                # All gates passed → terminate cleanly. Set final_text,
                # emit the terminate event, flip the two-level break flag,
                # and break the inner tool_calls loop. The outer step loop
                # checks `respond_terminated` immediately after the inner
                # loop ends so post-dispatch gates (post_write_idle,
                # consecutive_repeat) do not re-fire on the partial step.
                result.final_text = message
                if log_calls:
                    append_event(
                        run_id, "respond_called",
                        phase=phase, step=step,
                        writes_seen=writes_seen,
                        message_chars=len(message),
                        compaction_max_phase=compaction_max_phase_this_run,
                    )
                respond_terminated = True
                break

            # Dispatch-time visibility: every other record (events.jsonl,
            # on_tool_event, /verbose) fires on COMPLETION, so a hung tool
            # was invisible everywhere — including post-hoc (session
            # 5bb630813c21: 9m40s on a curl with no trace of the command).
            logger.debug("tool dispatch step=%d name=%s args=%s",
                         step + 1, tc.name, _args_preview(tc.arguments))
            executed = dispatch_tool(
                tc.name, tc.arguments, tool_fns,
                cache=cache, cacheable=cacheable,
            )
            logger.debug("tool done name=%s wall_s=%.2f error=%s bytes_out=%d",
                         tc.name, getattr(executed, "wall_s", 0.0) or 0.0,
                         (getattr(executed, "error", None) or "")[:200] or None,
                         getattr(executed, "bytes_out", 0) or 0)
            if (log_calls and executed.error
                    and executed.error.startswith("Unknown tool")):
                append_event(
                    run_id, "tool_reject",
                    phase=phase, step=step, name=tc.name,
                    reason="unknown_tool", message=executed.error[:300],
                )
            result.tool_calls.append(executed)
            # SpecDD Lever 1: track every successfully-dispatched call (name,
            # args) for the spec validator. Skip error and schema-reject
            # cases — those don't represent a "real" call as far as the
            # agent-trajectory predicates are concerned.
            if not executed.error:
                actual_tool_calls.append((tc.name, tc.arguments))
            seen_calls.add(key)
            # v1.10 — append to tool_history for the convergence score.
            # Bounded to the last TOOL_HISTORY_MAX entries; only
            # successfully-dispatched calls (errors don't represent observed
            # behavior). Path extraction is permissive — see
            # luxe.agents.convergence.extract_path.
            if not executed.error:
                tool_history.append({
                    "step": step,
                    "name": tc.name,
                    "path": extract_path(tc.name, tc.arguments),
                })
                if len(tool_history) > TOOL_HISTORY_MAX:
                    tool_history = tool_history[-TOOL_HISTORY_MAX:]
            if tc.name in _WRITE_TOOLS and not executed.error:
                writes_seen += 1
                post_write_idle_tools = 0
                # forge-hybrid Phase 3 (B1) — track first/last write step
                # for the respond terminal-tool watchdogs (passive-surrender
                # gate inspects last_write_step). Unconditional bookkeeping;
                # used only when respond_terminal_enabled is True.
                if first_write_step is None:
                    first_write_step = step
                last_write_step = step
                # forge-hybrid Phase 2 (A) — capture compaction phase at the
                # first successful write. Used by the resolve-time telemetry
                # to attribute write-step gating to compaction state. Fires
                # at most once per run.
                if (tiered_compact_enabled
                        and compaction_phase_at_first_write is None):
                    compaction_phase_at_first_write = compaction_max_phase_this_run
                    if log_calls:
                        append_event(
                            run_id, "compaction_phase_at_first_write",
                            phase=phase, step=step,
                            phase_reached=compaction_phase_at_first_write,
                        )
                # v1.10 — post-intervention write telemetry. Capture
                # time-to-first-write and sustained-write-burst signals
                # for any trajectory where an intervention fired earlier.
                if last_intervention_step is not None:
                    if first_write_step_after_intervention is None:
                        first_write_step_after_intervention = step - last_intervention_step
                    post_intervention_consecutive_writes += 1
                    if post_intervention_consecutive_writes > post_intervention_write_burst_max:
                        post_intervention_write_burst_max = post_intervention_consecutive_writes
            elif writes_seen > 0:
                # A repeat returns content, so without the opt-in it RESETS
                # the streak and the guard never arms. Demonstrated on m1
                # 2026-08-10 (Qwen3.6-35B-A3B-4bit code drill): step 1 reads
                # key f0ee19e9; after the edit at step 9, step 10 reads the
                # SAME key again — dup=False (read_file is dedup-exempt),
                # bytes=78 — so the streak reset. Neither this guard nor
                # consecutive_repeat could see it.
                idle = executed.bytes_out == 0 or executed.error
                if post_write_idle_repeats and call_is_repeat:
                    idle = True
                if idle:
                    post_write_idle_tools += 1
                else:
                    post_write_idle_tools = 0
                # v1.10 — non-write after intervention breaks the burst
                # (only matters once at least one write has occurred).
                if last_intervention_step is not None:
                    post_intervention_consecutive_writes = 0

            content = executed.error or executed.result
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id or f"call_{step}",
                "name": tc.name,
                "content": content,
            })

            if on_tool_event:
                on_tool_event(executed)
            if log_calls:
                append_event(
                    run_id, "tool_call",
                    phase=phase, step=step, name=tc.name,
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
                if compaction_max_phase_this_run > 0:
                    recovery_event = _COMPACTION_RECOVERY_EVENT_BY_TOOL.get(tc.name)
                    if recovery_event is not None:
                        append_event(
                            run_id, recovery_event,
                            phase=phase, step=step, name=tc.name,
                            compaction_max_phase=compaction_max_phase_this_run,
                        )

        # forge-hybrid Phase 3 (B1) — clean two-level exit. The inner
        # tool_calls loop broke with respond_terminated set; the model's
        # final message is already on result.final_text. Skip the
        # post-dispatch gates (post_write_idle_exit, consecutive_repeat)
        # so they don't re-fire on the partial step, and break the outer
        # step loop without setting result.aborted.
        if respond_terminated:
            break

        pwi_exit = PostWriteIdleExitGuard.should_exit(
            post_write_idle_tools=post_write_idle_tools,
            writes_seen=writes_seen,
        )
        if pwi_exit is not None:
            result.final_text = resp.text or ""
            if log_calls:
                append_event(
                    run_id, "post_write_idle_exit",
                    phase=phase, step=step,
                    **pwi_exit,
                )
            break

        if step_had_repeat:
            consecutive_repeat_steps += 1
            if log_calls:
                append_event(
                    run_id, "tool_step_done",
                    phase=phase, step=step,
                    step_had_repeat=True,
                    consecutive_repeat_steps=consecutive_repeat_steps,
                )
            cr_abort = ConsecutiveRepeatGuard.should_abort(
                consecutive_repeat_steps=consecutive_repeat_steps,
            )
            if cr_abort is not None:
                result.final_text = resp.text or ""
                result.aborted = True
                result.abort_reason = cr_abort["abort_reason"]
                break
        else:
            consecutive_repeat_steps = 0
            if log_calls:
                append_event(
                    run_id, "tool_step_done",
                    phase=phase, step=step,
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
    if tiered_compact_enabled and log_calls:
        append_event(
            run_id, "compaction_phase_at_resolve",
            phase=phase,
            max_phase_reached=compaction_max_phase_this_run,
            phase_at_first_write=compaction_phase_at_first_write,
            tool_results_dropped_total=compaction_tool_results_dropped_total,
            total_tokens_dropped=compaction_total_tokens_dropped,
            aborted=result.aborted,
        )

    result.wall_s = time.monotonic() - t0
    return result
