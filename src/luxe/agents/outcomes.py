"""Episode-outcome taxonomy — first-class failure classification.

Reads an agent run's `events.jsonl` + final AgentResult/predictions state
and emits a structured outcome record with three fields:

  - `outcome`: single tag from `Outcome` enum (final tier)
  - `interventions_fired`: list of intervention names that triggered
  - `failure_chain`: ordered list of primary→terminal classes, or None
    if the outcome is success

Decoupling outcomes from interventions (per v1.8 plan feedback) avoids
combinatoric label explosion. A run can have `outcome=PLAUSIBLE_EDIT`
AND `interventions_fired=[WRITE_PRESSURE, EARLY_BAIL]` — both dimensions
are independently queryable.

Backfillable: works on existing v17 events.jsonl without re-running.

Used by:
  - `benchmarks/*/run.py`: emit aggregate `failure_classes` field in
    summary.json
  - `scripts/classify_run.py` (separate CLI tool): one-shot
    classification for ad-hoc analysis
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


# --- Outcome enum -----------------------------------------------------------
#
# Atoms are mutually exclusive and cover the full success / failure space.
# Order is roughly best→worst within each major bucket.


class Outcome(str, Enum):
    # Success / partial success
    STRONG_GOLD_MATCH = "STRONG_GOLD_MATCH"  # SWE-bench: file + hunk + shape match
    PLAUSIBLE_EDIT = "PLAUSIBLE_EDIT"        # SWE-bench: edit landed, may not match gold
    CORRECT_ABSTAIN = "CORRECT_ABSTAIN"      # BFCL irrelevance: zero calls, prose decline
    MULTI_TOOL_COMPLETE = "MULTI_TOOL_COMPLETE"  # BFCL parallel*: all expected calls emitted
    SINGLE_TOOL_CORRECT = "SINGLE_TOOL_CORRECT"  # BFCL simple/multiple: correct call

    # Wrong-shape outcomes (edit landed but missed)
    WRONG_TARGET = "WRONG_TARGET"            # SWE-bench: edited wrong file
    WRONG_LOCATION = "WRONG_LOCATION"        # SWE-bench: right file, wrong locus
    MULTI_TOOL_ORDERING_FAILURE = "MULTI_TOOL_ORDERING_FAILURE"  # BFCL parallel*: partial coverage

    # Failure-to-act outcomes (no edit / no call)
    EMPTY_PATCH_TIMEOUT = "EMPTY_PATCH_TIMEOUT"          # SWE-bench: no patch, max_steps or natural exit
    EMPTY_PATCH_CONTEXT_EXHAUSTED = "EMPTY_PATCH_CONTEXT_EXHAUSTED"  # SWE-bench: oMLX 400 prompt size
    FORBIDDEN_TOOL_EMISSION = "FORBIDDEN_TOOL_EMISSION"  # BFCL irrelevance: model called tool
    STUCK_LOOP = "STUCK_LOOP"                            # any: _MAX_CONSECUTIVE_REPEAT_STEPS

    # Catch-all
    UNCLASSIFIED = "UNCLASSIFIED"


# --- Intervention enum (metadata, not outcome) -----------------------------


class Intervention(str, Enum):
    WRITE_PRESSURE = "WRITE_PRESSURE"
    EARLY_BAIL = "EARLY_BAIL"
    PROSE_BURST = "PROSE_BURST"
    SPEC_GATE_EXPECTS_ZERO = "SPEC_GATE_EXPECTS_ZERO"
    SPEC_GATE_MIN_TOOL_CALLS = "SPEC_GATE_MIN_TOOL_CALLS"


# --- Failure-class enum (chain primitives) ---------------------------------
#
# These are the CAUSAL classes that may appear in `failure_chain`. They
# differ from `Outcome` in that a run can transit through multiple
# (e.g. EARLY_PROSE_COLLAPSE → EMPTY_PATCH_TIMEOUT). Outcomes are
# terminal; failure classes are causal links.


class FailureClass(str, Enum):
    EARLY_PROSE_COLLAPSE = "EARLY_PROSE_COLLAPSE"        # step ≤4, big prose, no action
    BAILOUT_AFTER_READS = "BAILOUT_AFTER_READS"          # step >4, many reads, no writes
    ABSTAIN_AFTER_INTERVENTION = "ABSTAIN_AFTER_INTERVENTION"  # took escape after EARLY_BAIL
    EMPTY_PATCH_TIMEOUT = "EMPTY_PATCH_TIMEOUT"          # exhausted max_steps
    CONTEXT_EXHAUSTED = "CONTEXT_EXHAUSTED"              # backend 400 on prompt size
    BACKEND_ERROR = "BACKEND_ERROR"                      # other backend failure
    STUCK_LOOP = "STUCK_LOOP"                            # dedup short-circuit fired
    FORBIDDEN_DISPATCH = "FORBIDDEN_DISPATCH"            # T2 gate blocked a call
    SCHEMA_REJECT = "SCHEMA_REJECT"                      # validate_args rejected


@dataclass(frozen=True)
class EpisodeOutcome:
    outcome: Outcome
    interventions_fired: list[Intervention] = field(default_factory=list)
    failure_chain: list[FailureClass] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "interventions_fired": [i.value for i in self.interventions_fired],
            "failure_chain": (
                [c.value for c in self.failure_chain]
                if self.failure_chain is not None else None
            ),
        }


# --- Trace parsing ---------------------------------------------------------


@dataclass
class _TraceSummary:
    """Compact view over events.jsonl needed for classification."""
    total_steps: int = 0
    first_step_completion_tokens: int = 0
    max_step_completion_tokens: int = 0
    total_tool_calls: int = 0
    write_tool_calls: int = 0
    duplicate_calls: int = 0
    schema_rejects: int = 0
    aborted: bool = False
    abort_reason: str = ""
    interventions: list[Intervention] = field(default_factory=list)
    pr_blocked: bool = False
    post_write_idle_exit: bool = False


def _parse_events(events_path: Path) -> _TraceSummary:
    summary = _TraceSummary()
    if not events_path.is_file():
        return summary
    write_tools = {"write_file", "edit_file"}
    seen_steps: set[int] = set()
    intervention_map = {
        "write_pressure_fired": Intervention.WRITE_PRESSURE,
        "early_bail_fired": Intervention.EARLY_BAIL,
        "prose_burst_fired": Intervention.PROSE_BURST,
    }
    last_completion_tokens = 0
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = evt.get("kind")
        if kind == "tool_call" and evt.get("phase") == "main":
            seen_steps.add(evt.get("step", 0))
            summary.total_tool_calls += 1
            name = (evt.get("name") or "").strip()
            if name in write_tools:
                summary.write_tool_calls += 1
            if evt.get("duplicate"):
                summary.duplicate_calls += 1
        elif kind in intervention_map:
            summary.interventions.append(intervention_map[kind])
            # Capture completion_tokens at intervention time as crude
            # per-step proxy (used by EARLY_PROSE_COLLAPSE classifier).
            ct = evt.get("completion_tokens", 0)
            if ct > last_completion_tokens:
                summary.first_step_completion_tokens = max(
                    summary.first_step_completion_tokens,
                    ct - last_completion_tokens,
                )
                last_completion_tokens = ct
        elif kind == "spec_reprompt_fired":
            req_kind = evt.get("requirement_kind")
            if req_kind == "expects_zero_calls":
                summary.interventions.append(Intervention.SPEC_GATE_EXPECTS_ZERO)
            elif req_kind == "min_tool_calls":
                summary.interventions.append(Intervention.SPEC_GATE_MIN_TOOL_CALLS)
            else:
                # Unknown predicate kind — record the generic spec-gate fire so
                # the run isn't silently dropped from intervention counts.
                summary.interventions.append(Intervention.SPEC_GATE_EXPECTS_ZERO)
        elif kind == "post_write_idle_exit":
            summary.post_write_idle_exit = True
        elif kind == "pr_blocked":
            summary.pr_blocked = True
        elif kind in ("single_mode_done", "agent_done"):
            summary.aborted = bool(evt.get("aborted"))
            summary.abort_reason = evt.get("abort_reason", "")
            summary.total_tool_calls = max(
                summary.total_tool_calls,
                evt.get("tool_calls_total", 0),
            )
            summary.schema_rejects = evt.get("schema_rejects", 0)
    summary.total_steps = len(seen_steps)
    return summary


# --- Classification --------------------------------------------------------


def _classify_swebench(
    summary: _TraceSummary,
    *,
    has_patch: bool,
    tier: str | None,
) -> EpisodeOutcome:
    """SWE-bench classification — uses smoke_inspect tier as outcome and
    derives failure_chain from trace shape.

    `tier` is one of: strong, plausible, wrong_target, wrong_location,
    empty_patch (passed in from the inspector).
    """
    chain: list[FailureClass] = []

    if has_patch and tier in ("strong", "plausible"):
        outcome = (Outcome.STRONG_GOLD_MATCH if tier == "strong"
                   else Outcome.PLAUSIBLE_EDIT)
        return EpisodeOutcome(outcome=outcome,
                              interventions_fired=summary.interventions,
                              failure_chain=None)

    if has_patch and tier == "wrong_target":
        return EpisodeOutcome(outcome=Outcome.WRONG_TARGET,
                              interventions_fired=summary.interventions,
                              failure_chain=None)

    if has_patch and tier == "wrong_location":
        return EpisodeOutcome(outcome=Outcome.WRONG_LOCATION,
                              interventions_fired=summary.interventions,
                              failure_chain=None)

    # No patch — derive failure chain
    if "max context window" in summary.abort_reason.lower() or \
       "prompt too long" in summary.abort_reason.lower():
        chain.append(FailureClass.CONTEXT_EXHAUSTED)
        return EpisodeOutcome(outcome=Outcome.EMPTY_PATCH_CONTEXT_EXHAUSTED,
                              interventions_fired=summary.interventions,
                              failure_chain=chain)

    if "stuck" in summary.abort_reason.lower() or summary.duplicate_calls > 0:
        chain.append(FailureClass.STUCK_LOOP)
        outcome = Outcome.STUCK_LOOP
        return EpisodeOutcome(outcome=outcome,
                              interventions_fired=summary.interventions,
                              failure_chain=chain)

    # Empty patch with no specific failure — derive primary from trace shape.
    # Early prose collapse: <=4 steps with zero writes.
    if summary.total_steps <= 4 and summary.write_tool_calls == 0 and summary.total_tool_calls <= 3:
        chain.append(FailureClass.EARLY_PROSE_COLLAPSE)
        chain.append(FailureClass.EMPTY_PATCH_TIMEOUT)
        return EpisodeOutcome(outcome=Outcome.EMPTY_PATCH_TIMEOUT,
                              interventions_fired=summary.interventions,
                              failure_chain=chain)

    # Many reads, no writes — bailout-after-reads class
    if summary.total_tool_calls >= 5 and summary.write_tool_calls == 0:
        chain.append(FailureClass.BAILOUT_AFTER_READS)
        # If EARLY_BAIL fired and we still didn't write, intervention failed
        if Intervention.EARLY_BAIL in summary.interventions:
            chain.append(FailureClass.ABSTAIN_AFTER_INTERVENTION)
        chain.append(FailureClass.EMPTY_PATCH_TIMEOUT)
        return EpisodeOutcome(outcome=Outcome.EMPTY_PATCH_TIMEOUT,
                              interventions_fired=summary.interventions,
                              failure_chain=chain)

    # Backend error catch-all
    if summary.abort_reason and "backend" in summary.abort_reason.lower():
        chain.append(FailureClass.BACKEND_ERROR)
        return EpisodeOutcome(outcome=Outcome.EMPTY_PATCH_TIMEOUT,
                              interventions_fired=summary.interventions,
                              failure_chain=chain)

    chain.append(FailureClass.EMPTY_PATCH_TIMEOUT)
    return EpisodeOutcome(outcome=Outcome.EMPTY_PATCH_TIMEOUT,
                          interventions_fired=summary.interventions,
                          failure_chain=chain)


def _classify_bfcl(
    summary: _TraceSummary,
    *,
    category: str,
    passed: bool,
    actual_call_count: int,
    expected_call_count: int | None,
) -> EpisodeOutcome:
    """BFCL classification — outcome reflects per-category grader semantics."""
    if category == "irrelevance":
        if passed:
            return EpisodeOutcome(outcome=Outcome.CORRECT_ABSTAIN,
                                  interventions_fired=summary.interventions,
                                  failure_chain=None)
        chain = [FailureClass.FORBIDDEN_DISPATCH]
        return EpisodeOutcome(outcome=Outcome.FORBIDDEN_TOOL_EMISSION,
                              interventions_fired=summary.interventions,
                              failure_chain=chain)

    if category in ("parallel", "parallel_multiple"):
        if passed:
            return EpisodeOutcome(outcome=Outcome.MULTI_TOOL_COMPLETE,
                                  interventions_fired=summary.interventions,
                                  failure_chain=None)
        # Failed parallel — distinguish "didn't make enough calls" from "wrong calls"
        if expected_call_count is not None and actual_call_count < expected_call_count:
            chain = [FailureClass.BAILOUT_AFTER_READS]
            return EpisodeOutcome(outcome=Outcome.MULTI_TOOL_ORDERING_FAILURE,
                                  interventions_fired=summary.interventions,
                                  failure_chain=chain)
        return EpisodeOutcome(outcome=Outcome.MULTI_TOOL_ORDERING_FAILURE,
                              interventions_fired=summary.interventions,
                              failure_chain=None)

    # simple_python / multiple
    if passed:
        return EpisodeOutcome(outcome=Outcome.SINGLE_TOOL_CORRECT,
                              interventions_fired=summary.interventions,
                              failure_chain=None)
    return EpisodeOutcome(outcome=Outcome.UNCLASSIFIED,
                          interventions_fired=summary.interventions,
                          failure_chain=None)


def classify_swebench_run(
    events_path: Path,
    *,
    has_patch: bool,
    tier: str | None,
) -> EpisodeOutcome:
    """Public entry point — SWE-bench instance to EpisodeOutcome."""
    summary = _parse_events(events_path)
    return _classify_swebench(summary, has_patch=has_patch, tier=tier)


def classify_bfcl_run(
    events_path: Path | None,
    *,
    category: str,
    passed: bool,
    actual_call_count: int,
    expected_call_count: int | None = None,
) -> EpisodeOutcome:
    """Public entry point — BFCL problem to EpisodeOutcome. `events_path`
    may be None for raw-mode runs (no events.jsonl) — interventions will
    be empty."""
    if events_path is not None and events_path.is_file():
        summary = _parse_events(events_path)
    else:
        summary = _TraceSummary()
    return _classify_bfcl(summary, category=category, passed=passed,
                          actual_call_count=actual_call_count,
                          expected_call_count=expected_call_count)


def aggregate_outcomes(outcomes: list[EpisodeOutcome]) -> dict[str, Any]:
    """Build a summary dict for `summary.json`. Counts outcomes,
    interventions, and failure chain heads."""
    out: dict[str, Any] = {
        "n_total": len(outcomes),
        "outcome_counts": {},
        "intervention_counts": {},
        "failure_chain_head_counts": {},
        "unclassified_rate": 0.0,
    }
    for ep in outcomes:
        out["outcome_counts"][ep.outcome.value] = (
            out["outcome_counts"].get(ep.outcome.value, 0) + 1)
        for intv in ep.interventions_fired:
            out["intervention_counts"][intv.value] = (
                out["intervention_counts"].get(intv.value, 0) + 1)
        if ep.failure_chain:
            head = ep.failure_chain[0].value
            out["failure_chain_head_counts"][head] = (
                out["failure_chain_head_counts"].get(head, 0) + 1)
    if outcomes:
        out["unclassified_rate"] = (
            out["outcome_counts"].get(Outcome.UNCLASSIFIED.value, 0) / len(outcomes)
        )
    return out
