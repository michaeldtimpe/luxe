"""Shared agent loop — tool dispatch, schema validation, telemetry.

Mirrors luxe's agents/base.py run_agent() pattern: chat → parse tool calls →
validate → dispatch → append results → repeat until done or budget exhausted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from luxe.agents.cohort_priors import load_prior_from_env
from luxe.agents.convergence import (
    _DEFAULT_MAX_DELTA,
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
from luxe.backend import Backend, ChatResponse, ToolCallResponse
from luxe.config import RoleConfig
from luxe.context import (
    TieredCompact,
    context_pressure,
    elide_old_tool_results,
)
from luxe.run_state import append_event
from luxe.spec import Spec
from luxe.spec_validator import validate as spec_validate
from luxe.tools.base import ToolCache, ToolDef, ToolCall, ToolFn, dispatch_tool, validate_args


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
    wall_s: float = 0.0
    peak_context_pressure: float = 0.0


OnToolEvent = Callable[[ToolCall], None]


def _parse_text_tool_calls(
    text: str,
    known_names: set[str],
) -> list[ToolCallResponse]:
    """Recover tool calls from text when model doesn't use structured output."""
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
        except (json.JSONDecodeError, KeyError):
            continue

    return calls


_MAX_CONSECUTIVE_REPEAT_STEPS = 2

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

# Post-write drift detector. Once at least one write has succeeded, any
# run of non-write tool calls that each return zero bytes (or hit the
# dedup short-circuit) signals "diff already produced, model is spinning
# on verification without new information." Exit cleanly at this many
# back-to-back idle calls so the harness still commits/pushes the work
# without burning the full max_steps budget.
# Observed pattern (qwen3-coder-next-80b, 2026-05-10 m5max_moe bake-off):
# edit_file (step 1) → git_diff 972B (step 2) → lint 0B → bash 0B →
# git_diff 0B (dup) → bash 0B (dup×2 → stuck_no_output bailout). Diff
# was already correct from step 1; steps 3–11 produced no new signal.
_POST_WRITE_IDLE_MAX = 3

# Mid-loop write-pressure thresholds (Mode B fix). Fires once per run when
# the gate hits: step >= MIN_STEP, tools >= MIN_TOOLS, zero writes, AND
# either completion >= MIN_TOKENS (prose-heavy trap) or tools >=
# MAX_TOOLS_BEFORE_FIRE (tool-heavy trap). The dual signal handles models
# with different output distributions: qwen3.6-35b generates ~9k completion
# tokens in the v1.4 prose-mode failure, while qwen3-coder-next-80b emits
# 30 reads with only ~1.5k completion tokens — same read-loop pathology,
# different surface telemetry. Off by default; enable per-run with
# LUXE_WRITE_PRESSURE=1 (or via runtime config).
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

# Early-bail intervention (v1.7 priority #1). Fires once per run when the
# agent has read enough to plan but hasn't written. Targets SWE-bench's
# "no_abort, zero writes" empty_patch class: 10 of 18 v3 paired-mechanism
# empties showed model clean-exiting with 8000+ completion tokens but no
# edits (long-trace bailers: psf-requests-6028, pydata-xarray-2905/6938,
# django-11734, mpl-13989, sphinx-10435, sphinx-10614 partial). Distinct
# from WRITE_PRESSURE (which fires later, on prose-mode trap with 10+ tool
# calls). Trace audit 2026-05-11: step >= 4 with reads >= 4 catches 7 of
# 10 no_abort cases + intercepts 3 stuck_loop and 1 max_steps cases BEFORE
# their terminal detectors fire. Off by default; enable with
# LUXE_EARLY_BAIL=1.
_EARLY_BAIL_MIN_STEP = 4
_EARLY_BAIL_MIN_READS = 4

# v1.8 Track 1 — per-step prose-burst detector. Targets the short-trace
# bailer class identified in the B.1 audit (3/18 v3 empties unreachable by
# early_bail's step≥4 rule because the model exited at step ≤3 with 8000+
# completion tokens — prose burst without action). The composite invariant
# is fired-once at the same checkpoint as early_bail/write_pressure:
#   step <= _PROSE_BURST_MAX_STEP (4)
#   AND tool_calls_total == 0
#   AND writes_seen == 0
#   AND completion_tokens_delta_last_step >= _PROSE_BURST_MIN_DELTA (1500)
# 1500 is materially below the observed pathological range (3000-4000/step
# in the v17 B.5 short-trace cases) so legitimate planning traces have
# margin. Anti-oscillation: if the model's RESPONSE to the intervention is
# ALSO a prose burst with zero tool calls, exit cleanly (not aborted) —
# the trajectory is non-steerable. Off by default; enable with
# LUXE_PROSE_BURST=1.
_PROSE_BURST_MAX_STEP = 4
_PROSE_BURST_MIN_DELTA = 1500

# v1.9 — LUXE_ACTION_DENSITY_GATE. Staged-escalation predicate that catches
# the post-bail short-stall class (model accepted early_bail at step 4 but
# produced no edit) AND the standalone "diffuse-reconnaissance" class
# (many tools, many tokens, no commit) that PROSE_BURST's zero-tool-call
# rule cannot reach. Thresholds derived from
# scripts/mine_action_density.py over v17 + v18 SWE-bench n=75 traces;
# see acceptance/v19_mining/THRESHOLD_DECISION.md.
#
# Two fire modes:
#   - standalone: early_bail never fired; gate stands on its own.
#   - post_bail_rescue: early_bail fired at least MIN_TURNS_AFTER_BAIL
#     turns ago AND no writes have happened since. Targets confidence-
#     collapse trajectories where the bail nudge wasn't enough.
#
# Convergence proxy (same_file_read_twice) acts as a skip — strong
# trajectories converge by re-reading the same file (3× the empty-class
# rate per the v18 distribution), so the gate suppresses itself when
# convergence is observed. Even without the proxy the chosen thresholds
# already show 0 careful-strong risk on the historical distribution;
# the proxy is a safety belt against future drift.
#
# Off by default. Enable with LUXE_ACTION_DENSITY_GATE=1.
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

_PROSE_BURST_MESSAGE = (
    "Mid-loop notice: your previous response generated significant text "
    "without invoking any tool. The deliverable for this task is a "
    "concrete action, not a written explanation. Your next response must "
    "either (a) emit a tool call to gather information you need, or "
    "(b) emit a write/edit tool call to commit your solution. Reasoning "
    "in text without calling a tool is not progress."
)

_EARLY_BAIL_MESSAGE = (
    "Mid-loop notice: you have explored the repository but haven't proposed "
    "any edits yet. Choose the single file most likely to need modification "
    "and produce a concrete diff now using `write_file` or `edit_file`. If "
    "after this exploration you believe the existing code is correct as-is, "
    "say so explicitly with the file path you investigated and the reason — "
    "do not continue reading."
)

# v1.8 Track 3 — no-abstain variant for tasks where the bug is known to
# exist (SWE-bench: every instance has a definitional bug + gold patch).
# Removes the "explicitly state correct" escape valve that caused 3
# wrong_target/wrong_location → empty_patch regressions in v17 B.5.
# Activated via env var LUXE_EARLY_BAIL_MODE=no_abstain or by passing
# `early_bail_message=` to run_agent. SWE-bench adapter sets the env var
# before invoking the luxe maintain subprocess; maintain_suite gets the
# default message (abstain is sometimes legitimate there).
_EARLY_BAIL_MESSAGE_NO_ABSTAIN = (
    "Mid-loop notice: you have explored the repository but haven't proposed "
    "any edits yet. The fix exists in this repository. Choose the single "
    "file most likely to need modification and produce a concrete diff now "
    "using `write_file` or `edit_file`. Do not continue reading; commit to "
    "an edit based on what you've already learned."
)

# v1.9 — soft-anchor variant. The v18 no_abstain text swapped v17's 3
# wrong→empty regressions for 2 strong→empty confidence-collapse bails
# (sphinx-10435, sympy-13031). Per acceptance/v18_taxonomy: both v18
# regressions had v17=STRONG_GOLD_MATCH under the default message — the
# trajectory had a viable target before no_abstain pushed the planner
# into stall.
#
# v1.10 wording iteration: dropped "rather than continuing broad
# exploration" trailer. v1.9 ARM 1 evidence showed Qwen3.6-35B-A3B-6bit
# interpreted the comparative ("rather than … exploration") as "wrap up
# now" — sphinx-10435 rep_2 terminated at step 6 with 832 tokens and 0
# writes after early_bail at step 4. Positive imperative ending
# preserves the commitment lever without the implicit "stop reading"
# signal.
#
# Design intent:
#   - Selection heuristic ("highest-probability … even if uncertain")
#     gives the planner permission to commit under uncertainty — the
#     decision-commitment lever no_abstain lacked.
#   - No abstain valve — keeps the v17 wrong→empty class closed.
#   - No declarative "fix exists" framing — no_abstain's existence
#     claim may have triggered the confidence collapse; soft-anchor
#     reframes as "commit to your best read".
#   - Multi-hunk friendly — "location" (not "single file / single
#     hunk") preserves focus without overconstraining legitimate
#     multi-hunk fixes.
#   - Positive imperative ending — closing with the action verb, not
#     a contrast against exploration.
_EARLY_BAIL_MESSAGE_SOFT_ANCHOR = (
    "Mid-loop notice: you have explored the repository but haven't proposed "
    "any edits yet. Based on what you've already read, choose the "
    "highest-probability bug location — even if uncertain — and emit a "
    "concrete patch now using `write_file` or `edit_file`. Commit to your "
    "best candidate."
)

# v1.10 — commit-imperative variant fired by the conditional-stacking
# gate when convergence_score >= HIGH (model has identified a target
# and rereads/grep-localizes/preview-before-write are all signaling
# it's ready to commit). Tighter than soft-anchor; positive imperative,
# narrow concrete next-step framing, zero mention of exploration.
# The v1.9 lesson: avoid "rather than X" comparative phrases — they
# read as "wrap up now" on Qwen3.6-35B-A3B-6bit.
_EARLY_BAIL_MESSAGE_COMMIT_IMPERATIVE = (
    "Mid-loop notice: your read pattern indicates you have identified "
    "the likely target. Commit to the most promising file and attempt "
    "the smallest viable corrective edit using `write_file` or "
    "`edit_file` now."
)

# v1.10.1 introduced an exploratory-support variant for the score<LOW band
# (replacing v1.10's silent suppression). v1.10.2 added diversity gating
# inside it. v1.10.3 reverted both back to silent suppression after the
# 3-rep variance baseline showed non-Pareto regression at the band level
# (pylint-6528 empty in 2/3 reps under the exploratory variant; see
# project_v1102_variance_baseline.md). Constant + dict entry removed
# with the dispatcher revert. Old event logs still carry
# msg_variant="exploratory" / "soft_anchor_low_diversity_fallback";
# outcomes.py preserves their CONFIDENCE_COLLAPSE classification for
# back-compat analysis.

# v1.10.4 — breadth_probe variant for the score<LOW band. Replaces
# v1.10.3's blanket silent-suppression with a hybrid first-event +
# count-based escalation rule. The v1.10.3 3-rep cohort-shift audit
# (project_v1103_hold_finding.md, audit_v1103_suppression.py) showed
# that 50% of HARMFUL trajectories under blanket silent-suppression
# had n_suppressions == 1 — the sphinx-10435 archetype (1 supp →
# soft_anchor at step 5 → empty_patch). Blanket silent removed the
# only nudge the trajectory needed.
#
# Design constraints (per memory):
#   - NO "rather than X" framing — reads as wrap-up (feedback memory).
#   - NO "keep reading more files" instruction — v1.10.1 wasted-runway
#     shape that broke matplotlib-14623.
#   - NO "commit now" / "edit now" wording — that's soft_anchor's role
#     in the mid band; firing it here would collapse trajectories that
#     haven't yet converged on a target.
#   - Short, neutral, conditional — offers two branches without
#     forcing one. Acknowledges the diffuse-recon state without
#     prescribing a single action.
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

# v1.10.4 — escalation count for the breadth_probe hybrid. After this
# many cumulative silent suppressions on a single trajectory, re-fire
# the breadth_probe message as a safety-net escalation. Catches the
# extreme-tail HARMFUL cases (matplotlib-25775: 7 suppressions before
# soft_anchor at step 11). N=3 fires the escalation at the 3rd
# suppression event in the trajectory; the first event already fired
# breadth_probe (per the hybrid first-event rule), so the 2nd and any
# steps after the 3rd remain silent. Derived from the
# audit_v1103_suppression.py HARMFUL count distribution: median
# n_suppressions == 2; 80% of HARMFUL had n_suppressions in {1, 2, 3}.
_BREADTH_PROBE_ESCALATION_COUNT = 3

# v1.10.5c — first-event breadth_probe gating predicate (REFINED again).
#
# History:
#   v1.10.5a (initial): (diversity<3 AND bm25==0) — failed; calibrated
#     from bad audit data. See project_v1105_predicate_probe_failure.md.
#   v1.10.5b (corrected): NOT (bm25>0 AND grep==0) — fixed sphinx-10323
#     but broke sympy-12419 (consecutive-repeat-loop death spiral).
#   v1.10.5c (this iteration, FINAL local-predicate attempt per user
#     precommit): add distinct_files>=2 clause to separate two
#     mechanism-distinct failure modes that share the bm25-without-grep
#     signature.
#
# Verified deterministic feature vectors at suppression #1 (step 4):
#   archetype          bm25  grep  dist_f  desired   reason
#   sphinx-10435        1     1     1      FIRE      grep present (NOT looping)
#   matplotlib-14623    1     1     1      FIRE      grep present (NOT looping)
#   psf-requests-5414   0     0     2      FIRE      no bm25 (NOT looping)
#   psf-requests-1921   0     1     1      FIRE      grep present
#   sphinx-10323        1     0     2      SUPPRESS  bm25-without-grep + multi-file
#                                                    (synthesis-wandering pathology)
#   sympy-12419         1     0     1      FIRE      bm25-without-grep BUT single-file
#                                                    (premature-loop-kill pathology;
#                                                     needs message as loop-state
#                                                     destabilizer)
#
# The breadth_probe message serves TWO distinct jobs:
#   (1) exploration broadening — directs the model toward case analysis
#   (2) loop-state destabilization — perturbs policy out of local
#       attractors before _MAX_CONSECUTIVE_REPEAT_STEPS=2 aborts
# sphinx-10323 needs suppression of (1) [over-exploration → bad commit].
# sympy-12419 needs (2) preserved [model gets stuck in 2-call repeat
# without the perturbation]. distinct_files=2 partitions topology: deeper
# reading already indicates breadth (suppress); single-file repetition
# means model still needs external perturbation (fire).
#
# Escalation at suppression #_BREADTH_PROBE_ESCALATION_COUNT is NOT
# gated on this predicate (different failure mode; targets matplotlib-25775).
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

# v1.10 — convergence-score thresholds for conditional intervention
# stacking. Picked to span the natural score range for the
# Qwen3.6-35B-A3B-6bit champion on SWE-bench:
#
#   - All-distinct paths, no greps, no edits → score = 0.00  → SUPPRESS
#   - 5 reads w/ 1 reread, low entropy           → score ≈ 0.18  → MID
#   - 4 reads of same file, no other signals     → score = 0.44  → HIGH
#   - Strong: reread + localized grep + preview  → score ≈ 0.75  → HIGH
#
# Below LOW: diffuse-recon; commitment-style interventions hurt
#            exploratory recovery paths (v1.9 ARM 1 lost matplotlib-25775,
#            requests-5414 to this pattern). Suppress.
# LOW ≤ score < HIGH: standard band; soft-anchor wording fires.
# Score ≥ HIGH: model has identified a target via repeated reads /
#            localized greps / preview-before-write. Fire tighter
#            commit_imperative variant; suppress the action-density
#            gate (model is converging on its own; rescue would interrupt).
#
# These are starting thresholds — v1.10 Item 2 includes a re-mining
# pass against v19 traces to refine them; see
# acceptance/v110_mining/THRESHOLD_DECISION.md.
_CONVERGENCE_LOW_THRESHOLD = 0.10
_CONVERGENCE_HIGH_THRESHOLD = 0.40

# v1.10.1 — habituation clean-exit predicate threshold. Calibrated against
# sympy-13031 trace evidence (the founding instance): all three distinct
# interventions fired by step 15, zero writes through step 30+. The
# trajectory is intervention-resistant — burning the remaining max_steps
# yields no further information. Exit at step ≥20 to give the model 5+
# steps after the typical third-intervention step before terminating.
_HABITUATION_EXIT_MIN_STEP = 20
_HABITUATION_EXIT_MIN_KINDS = 3

# v1.10.2 — post-exploratory escalation was implemented and tested but
# REMOVED before ship. The n=4 probe revealed it regressed
# matplotlib-14623 (cascading through 3 interventions into
# habituation_exit) while rescuing pylint-6528. Single-mechanism
# escalation can't satisfy both at this convergence-score band.
# v1.10.3 needs trajectory-shape signals (e.g. post-bail tool_call
# rate, fraction of grep vs read in the rescue window) not a single
# step-based predicate. See lessons.md 2026-05-15 entry.

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
    """

    result = AgentResult()
    t0 = time.monotonic()
    # v1.10.1 — log_calls default-on when run_id is set. Earlier policy
    # was opt-in via LUXE_LOG_TOOL_CALLS=1, which silently degraded the
    # v1.10 production taxonomy (intervention fires + tool_calls invisible)
    # for any run that didn't have the env exported. Default-on closes
    # the footgun the v1.10 audit caught. Opt out via LUXE_SUPPRESS_TOOL_LOG=1
    # (ablation parity for legacy callers).
    log_calls = bool(run_id) and os.environ.get("LUXE_SUPPRESS_TOOL_LOG") != "1"

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
    write_pressure_enabled = os.environ.get("LUXE_WRITE_PRESSURE") == "1"
    write_pressure_fired = False
    early_bail_enabled = os.environ.get("LUXE_EARLY_BAIL") == "1"
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
    early_bail_commit_only = os.environ.get("LUXE_EARLY_BAIL_COMMIT_ONLY") == "1"
    prose_burst_enabled = os.environ.get("LUXE_PROSE_BURST") == "1"
    prose_burst_fired = False
    # v1.9 — LUXE_ACTION_DENSITY_GATE (staged escalation second-stage rescue
    # after early_bail stalls). See _ACTION_DENSITY_GATE_* constants above.
    action_density_gate_enabled = os.environ.get("LUXE_ACTION_DENSITY_GATE") == "1"
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
    convergence_gate_enabled = os.environ.get("LUXE_CONVERGENCE_GATE") == "1"
    # forge-hybrid Phase 2 (A) — TieredCompact context compaction. Default OFF
    # (byte-identical baseline preserved via the existing elide_old_tool_results
    # fallback). When LUXE_TIERED_COMPACT=1, the 3-phase compaction strategy
    # replaces elide at the pre-chat compaction site. Run-cumulative counters
    # below feed the resolve-time telemetry event.
    tiered_compact_enabled = os.environ.get("LUXE_TIERED_COMPACT") == "1"
    # Override the default compact_threshold (0.75) for stress-testing. Lower
    # values force compaction to fire at lower context pressure — useful for
    # surfacing the lever's behavior on workloads that rarely hit the default
    # trigger. Out-of-band values (<=0 or >=1) silently fall back to default.
    try:
        _tc_threshold = float(os.environ.get("LUXE_TIERED_COMPACT_THRESHOLD", "0.75"))
        if not (0.0 < _tc_threshold < 1.0):
            _tc_threshold = 0.75
    except ValueError:
        _tc_threshold = 0.75
    # Per-phase override: comma-separated "p1,p2,p3" (e.g., "0.50,0.85,0.95").
    # When set + valid, overrides the single-threshold knob. Mirrors forge's
    # TieredCompact.phase_thresholds. Malformed values silently fall back.
    _tc_phase_thresholds: tuple[float, float, float] | None = None
    _phase_raw = os.environ.get("LUXE_TIERED_COMPACT_PHASE_THRESHOLDS", "")
    if _phase_raw:
        try:
            _parts = [float(x.strip()) for x in _phase_raw.split(",")]
            if len(_parts) == 3 and all(0.0 < p < 1.0 for p in _parts):
                _tc_phase_thresholds = (_parts[0], _parts[1], _parts[2])
        except ValueError:
            pass
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
    last_compaction_phase: int = 0  # latest phase >0; reset to 0 only on resolve event

    # forge-hybrid Phase 3 (B1) — respond terminal tool. Default OFF
    # (byte-identical baseline preserved). When LUXE_RESPOND_TERMINAL=1,
    # the model can call respond(message=...) to exit the loop, gated by
    # 4 watchdogs (compaction-phantom, early-respond, no-writes-late,
    # passive-surrender). See src/luxe/tools/respond.py + tools.sdd.
    respond_terminal_enabled = os.environ.get("LUXE_RESPOND_TERMINAL") == "1"
    first_write_step: int | None = None
    last_write_step: int | None = None
    respond_terminated = False

    # v1.11 Phase 1 — adaptive policy substrate. Computation + observability
    # ONLY in Phase 1; modulation does NOT yet influence intervention
    # dispatch (deferred to Phase 3a so any behavior change is gated under
    # archetype-probe testing). Disable-equivalence invariant: when
    # LUXE_ADAPTIVE_POLICY=0 (or unset), zero adaptive_state events are
    # emitted and zero new state is computed — v1.10.5 byte-identical.
    adaptive_policy_enabled = os.environ.get("LUXE_ADAPTIVE_POLICY") == "1"
    # Per-signal ablation toggles (default ON when adaptive_policy_enabled).
    adaptive_no_write_enabled = os.environ.get("LUXE_ADAPTIVE_NO_WRITE", "1") == "1"
    adaptive_score_trend_enabled = os.environ.get("LUXE_ADAPTIVE_SCORE_TREND", "1") == "1"
    # v1.11 Phase 3a — slew-rate limit; agents.sdd-pinned default 0.3.
    # Bounds per-step intensity-modifier change. Override for ablation.
    try:
        adaptive_max_delta = float(
            os.environ.get("LUXE_ADAPTIVE_MAX_INTENSITY_DELTA_PER_STEP", "")
            or _DEFAULT_MAX_DELTA
        )
    except ValueError:
        adaptive_max_delta = _DEFAULT_MAX_DELTA
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
    _band_response = os.environ.get(
        "LUXE_EARLY_BAIL_BAND_RESPONSE", "breadth_probe_hybrid")
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

    for step in range(role_cfg.max_steps):
        result.steps = step + 1

        pressure = context_pressure(messages, role_cfg.num_ctx)
        result.peak_context_pressure = max(result.peak_context_pressure, pressure)

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
            result.final_text = resp.text or ""
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
            result.final_text = resp.text or "" if 'resp' in dir() else ""
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
            cr = _tiered_compactor.compact(messages, role_cfg.num_ctx)
            messages = cr.messages
            if cr.phase_reached > 0:
                compaction_tool_results_dropped_total += cr.tool_results_dropped
                compaction_total_tokens_dropped += (cr.tokens_before - cr.tokens_after)
                if cr.phase_reached > compaction_max_phase_this_run:
                    compaction_max_phase_this_run = cr.phase_reached
                last_compaction_phase = cr.phase_reached
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
            messages = elide_old_tool_results(messages, role_cfg.num_ctx)

        try:
            resp: ChatResponse = backend.chat(
                messages,
                tools=openai_tools,
                max_tokens=role_cfg.max_tokens_per_turn,
                temperature=role_cfg.temperature,
                num_ctx=role_cfg.num_ctx,
                repeat_penalty=role_cfg.repeat_penalty,
            )
        except Exception as e:
            result.aborted = True
            result.abort_reason = f"Backend error: {e}"
            break

        result.prompt_tokens += resp.timing.prompt_tokens
        result.completion_tokens += resp.timing.completion_tokens

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
            tool_calls = _parse_text_tool_calls(resp.text, known_names)

        if not tool_calls:
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

            executed = dispatch_tool(
                tc.name, tc.arguments, tool_fns,
                cache=cache, cacheable=cacheable,
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
                if executed.bytes_out == 0 or executed.error:
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
        result.final_text = resp.text if 'resp' in dir() else ""
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
