"""Tests for src/luxe/agents/outcomes.py — v1.8 Track 5 taxonomy logger.

Classification logic over synthetic events + final state. The taxonomy
must be deterministic, mutually exclusive on outcomes, and produce
short failure_chain lists when applicable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from luxe.agents.outcomes import (
    FailureClass,
    Intervention,
    Outcome,
    aggregate_outcomes,
    classify_bfcl_run,
    classify_swebench_run,
)


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


# --- SWE-bench classification ---------------------------------------------


def test_swebench_strong_match(tmp_path):
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        {"kind": "tool_call", "phase": "main", "step": 0, "name": "read_file"},
        {"kind": "tool_call", "phase": "main", "step": 1, "name": "edit_file"},
        {"kind": "single_mode_done", "aborted": False, "tool_calls_total": 2},
    ])
    ep = classify_swebench_run(events_path, has_patch=True, tier="strong")
    assert ep.outcome == Outcome.STRONG_GOLD_MATCH
    assert ep.failure_chain is None


def test_swebench_plausible(tmp_path):
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        {"kind": "tool_call", "phase": "main", "step": 0, "name": "edit_file"},
        {"kind": "single_mode_done", "aborted": False, "tool_calls_total": 1},
    ])
    ep = classify_swebench_run(events_path, has_patch=True, tier="plausible")
    assert ep.outcome == Outcome.PLAUSIBLE_EDIT
    assert ep.failure_chain is None


def test_swebench_wrong_target(tmp_path):
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        {"kind": "tool_call", "phase": "main", "step": 0, "name": "edit_file"},
        {"kind": "single_mode_done", "aborted": False, "tool_calls_total": 1},
    ])
    ep = classify_swebench_run(events_path, has_patch=True, tier="wrong_target")
    assert ep.outcome == Outcome.WRONG_TARGET


def test_swebench_empty_context_exhausted(tmp_path):
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        {"kind": "tool_call", "phase": "main", "step": 0, "name": "read_file"},
        {"kind": "tool_call", "phase": "main", "step": 1, "name": "read_file"},
        {"kind": "single_mode_done", "aborted": True,
         "abort_reason": "Backend error: oMLX returned 400: Prompt too long: 45264 tokens exceeds max context window of 32768",
         "tool_calls_total": 2},
    ])
    ep = classify_swebench_run(events_path, has_patch=False, tier="empty_patch")
    assert ep.outcome == Outcome.EMPTY_PATCH_CONTEXT_EXHAUSTED
    assert FailureClass.CONTEXT_EXHAUSTED in ep.failure_chain


def test_swebench_early_prose_collapse(tmp_path):
    """Step ≤4, few tool calls, zero writes → primary class is
    EARLY_PROSE_COLLAPSE; secondary is EMPTY_PATCH_TIMEOUT."""
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        {"kind": "tool_call", "phase": "main", "step": 0, "name": "read_file"},
        {"kind": "tool_call", "phase": "main", "step": 1, "name": "read_file"},
        {"kind": "single_mode_done", "aborted": False, "tool_calls_total": 2},
    ])
    ep = classify_swebench_run(events_path, has_patch=False, tier="empty_patch")
    assert ep.outcome == Outcome.EMPTY_PATCH_TIMEOUT
    assert ep.failure_chain == [FailureClass.EARLY_PROSE_COLLAPSE,
                                FailureClass.EMPTY_PATCH_TIMEOUT]


def test_swebench_bailout_after_reads(tmp_path):
    """Many reads, no writes — BAILOUT_AFTER_READS primary."""
    events_path = tmp_path / "events.jsonl"
    events = [
        {"kind": "tool_call", "phase": "main", "step": i, "name": "read_file"}
        for i in range(8)
    ] + [{"kind": "single_mode_done", "aborted": False, "tool_calls_total": 8}]
    _write_events(events_path, events)
    ep = classify_swebench_run(events_path, has_patch=False, tier="empty_patch")
    assert ep.outcome == Outcome.EMPTY_PATCH_TIMEOUT
    assert ep.failure_chain[0] == FailureClass.BAILOUT_AFTER_READS


def test_swebench_bailout_with_failed_intervention(tmp_path):
    """EARLY_BAIL fired but model still didn't write → ABSTAIN_AFTER_INTERVENTION
    appears in chain."""
    events_path = tmp_path / "events.jsonl"
    events = [
        {"kind": "tool_call", "phase": "main", "step": i, "name": "read_file"}
        for i in range(8)
    ] + [
        {"kind": "early_bail_fired", "step": 4, "completion_tokens": 2000},
        {"kind": "single_mode_done", "aborted": False, "tool_calls_total": 8},
    ]
    _write_events(events_path, events)
    ep = classify_swebench_run(events_path, has_patch=False, tier="empty_patch")
    assert Intervention.EARLY_BAIL in ep.interventions_fired
    assert FailureClass.ABSTAIN_AFTER_INTERVENTION in ep.failure_chain


def test_swebench_stuck_loop(tmp_path):
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        {"kind": "tool_call", "phase": "main", "step": 0, "name": "read_file"},
        {"kind": "tool_call", "phase": "main", "step": 1, "name": "read_file", "duplicate": True},
        {"kind": "single_mode_done", "aborted": True,
         "abort_reason": "Stuck in loop — repeated same tool calls 2 consecutive turns",
         "tool_calls_total": 2},
    ])
    ep = classify_swebench_run(events_path, has_patch=False, tier="empty_patch")
    assert ep.outcome == Outcome.STUCK_LOOP
    assert FailureClass.STUCK_LOOP in ep.failure_chain


# --- BFCL classification --------------------------------------------------


def test_bfcl_correct_abstain():
    ep = classify_bfcl_run(None, category="irrelevance", passed=True,
                           actual_call_count=0)
    assert ep.outcome == Outcome.CORRECT_ABSTAIN
    assert ep.failure_chain is None


def test_bfcl_forbidden_tool_emission():
    ep = classify_bfcl_run(None, category="irrelevance", passed=False,
                           actual_call_count=1)
    assert ep.outcome == Outcome.FORBIDDEN_TOOL_EMISSION
    assert ep.failure_chain == [FailureClass.FORBIDDEN_DISPATCH]


def test_bfcl_multi_tool_complete():
    ep = classify_bfcl_run(None, category="parallel_multiple", passed=True,
                           actual_call_count=3, expected_call_count=3)
    assert ep.outcome == Outcome.MULTI_TOOL_COMPLETE


def test_bfcl_multi_tool_ordering_failure():
    ep = classify_bfcl_run(None, category="parallel_multiple", passed=False,
                           actual_call_count=1, expected_call_count=3)
    assert ep.outcome == Outcome.MULTI_TOOL_ORDERING_FAILURE


def test_bfcl_single_tool_correct():
    ep = classify_bfcl_run(None, category="simple_python", passed=True,
                           actual_call_count=1)
    assert ep.outcome == Outcome.SINGLE_TOOL_CORRECT


# --- Interventions metadata -----------------------------------------------


def test_interventions_separated_from_outcome(tmp_path):
    """A run with PLAUSIBLE_EDIT outcome AND interventions fired keeps
    them as distinct fields, not mashed into a single label."""
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        {"kind": "tool_call", "phase": "main", "step": 0, "name": "read_file"},
        {"kind": "write_pressure_fired", "step": 5, "completion_tokens": 4000},
        {"kind": "tool_call", "phase": "main", "step": 6, "name": "edit_file"},
        {"kind": "single_mode_done", "aborted": False, "tool_calls_total": 2},
    ])
    ep = classify_swebench_run(events_path, has_patch=True, tier="plausible")
    assert ep.outcome == Outcome.PLAUSIBLE_EDIT
    assert Intervention.WRITE_PRESSURE in ep.interventions_fired


def test_multiple_interventions_captured(tmp_path):
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [
        {"kind": "early_bail_fired", "step": 4, "completion_tokens": 1500},
        {"kind": "write_pressure_fired", "step": 6, "completion_tokens": 4500},
        {"kind": "tool_call", "phase": "main", "step": 7, "name": "edit_file"},
        {"kind": "single_mode_done", "aborted": False, "tool_calls_total": 1},
    ])
    ep = classify_swebench_run(events_path, has_patch=True, tier="strong")
    assert Intervention.EARLY_BAIL in ep.interventions_fired
    assert Intervention.WRITE_PRESSURE in ep.interventions_fired


# --- Aggregation ----------------------------------------------------------


def test_aggregate_outcomes_counts_and_intervention_dimension(tmp_path):
    eps = [
        classify_bfcl_run(None, category="irrelevance", passed=True,
                          actual_call_count=0),
        classify_bfcl_run(None, category="irrelevance", passed=True,
                          actual_call_count=0),
        classify_bfcl_run(None, category="irrelevance", passed=False,
                          actual_call_count=1),
    ]
    agg = aggregate_outcomes(eps)
    assert agg["n_total"] == 3
    assert agg["outcome_counts"]["CORRECT_ABSTAIN"] == 2
    assert agg["outcome_counts"]["FORBIDDEN_TOOL_EMISSION"] == 1


def test_aggregate_failure_chain_head_counts(tmp_path):
    events_path_a = tmp_path / "a.jsonl"
    events_path_b = tmp_path / "b.jsonl"
    # Both have EARLY_PROSE_COLLAPSE as primary
    _write_events(events_path_a, [
        {"kind": "tool_call", "phase": "main", "step": 0, "name": "read_file"},
        {"kind": "single_mode_done", "aborted": False, "tool_calls_total": 1},
    ])
    _write_events(events_path_b, [
        {"kind": "tool_call", "phase": "main", "step": 0, "name": "read_file"},
        {"kind": "tool_call", "phase": "main", "step": 1, "name": "read_file"},
        {"kind": "single_mode_done", "aborted": False, "tool_calls_total": 2},
    ])
    eps = [
        classify_swebench_run(events_path_a, has_patch=False, tier="empty_patch"),
        classify_swebench_run(events_path_b, has_patch=False, tier="empty_patch"),
    ]
    agg = aggregate_outcomes(eps)
    assert agg["failure_chain_head_counts"]["EARLY_PROSE_COLLAPSE"] == 2


def test_unclassified_rate_tracked():
    eps = [
        # simple_python with passed=False emits UNCLASSIFIED
        classify_bfcl_run(None, category="simple_python", passed=False,
                          actual_call_count=0),
        classify_bfcl_run(None, category="simple_python", passed=True,
                          actual_call_count=1),
    ]
    agg = aggregate_outcomes(eps)
    assert agg["unclassified_rate"] == 0.5


def test_outcome_dict_round_trip():
    ep = classify_bfcl_run(None, category="irrelevance", passed=True,
                           actual_call_count=0)
    d = ep.to_dict()
    assert d["outcome"] == "CORRECT_ABSTAIN"
    assert d["interventions_fired"] == []
    assert d["failure_chain"] is None
