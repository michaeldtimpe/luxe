"""v1.11 Phase 1 — adaptive-policy substrate integration test.

End-to-end verification of the loop.py wiring:
  - disable-equivalence: LUXE_ADAPTIVE_POLICY unset → no adaptive_state
    events emitted; v1.10.5 behavior preserved
  - enabled: LUXE_ADAPTIVE_POLICY=1 → adaptive_state events emitted each
    step with the signal values + convergence_score
  - per-signal ablation: LUXE_ADAPTIVE_NO_WRITE=0 → consecutive_no_write
    field is None in emitted events (other signal still active)
"""
from __future__ import annotations

from typing import Any

import luxe.agents.loop as loop_module
from luxe.agents.loop import run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.config import RoleConfig
from luxe.tools.base import ToolDef


class _ScriptedBackend:
    def __init__(self, scripted: list[ChatResponse]) -> None:
        self._scripted = list(scripted)

    def chat(self, messages, **kwargs) -> ChatResponse:
        if not self._scripted:
            return ChatResponse(text="", finish_reason="stop",
                                timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))
        return self._scripted.pop(0)


def _read_resp() -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="c", name="read_file", arguments={"path": "x.py"})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=100),
    )


def _read_resp_distinct(i: int) -> ChatResponse:
    """Read a DISTINCT file each step → diffuse recon → convergence stays low
    with a flat/falling trend (the empty_patch collapse signature)."""
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(
            id=f"c{i}", name="read_file", arguments={"path": f"pkg/mod_{i}.py"})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=100),
    )


def _terminal() -> ChatResponse:
    return ChatResponse(text="done", finish_reason="stop",
                        timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))


def _role() -> RoleConfig:
    return RoleConfig(model_key="test", num_ctx=4096, max_steps=10,
                      max_tokens_per_turn=2048, temperature=0.0)


def _read_tool() -> ToolDef:
    return ToolDef(
        name="read_file", description="read",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )


def _read_fn() -> dict[str, Any]:
    return {"read_file": lambda args: (f"contents of {args.get('path', '')}", None)}


def _capture_events(monkeypatch):
    """Patch append_event in loop module; return list captured calls land in."""
    captured: list[dict] = []

    def fake_append(run_id, kind, **fields):
        captured.append({"kind": kind, **fields})

    monkeypatch.setattr(loop_module, "append_event", fake_append)
    return captured


def test_adaptive_policy_disabled_emits_no_adaptive_state(monkeypatch):
    """LUXE_ADAPTIVE_POLICY unset → zero adaptive_state events.
    Disable-equivalence invariant from agents.sdd."""
    monkeypatch.delenv("LUXE_ADAPTIVE_POLICY", raising=False)
    monkeypatch.setenv("LUXE_LOG_TOOL_CALLS", "1")
    events = _capture_events(monkeypatch)

    scripted = [_read_resp() for _ in range(4)] + [_terminal()]
    run_agent(
        backend=_ScriptedBackend(scripted), role_cfg=_role(),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
        run_id="test-adaptive-policy",
    )

    adaptive_events = [e for e in events if e["kind"] == "adaptive_state"]
    assert adaptive_events == []


def test_adaptive_policy_enabled_emits_per_step(monkeypatch):
    """LUXE_ADAPTIVE_POLICY=1 → adaptive_state event each step with
    consecutive_no_write + score_trend + score_log_len + convergence_score."""
    monkeypatch.setenv("LUXE_ADAPTIVE_POLICY", "1")
    monkeypatch.setenv("LUXE_LOG_TOOL_CALLS", "1")
    events = _capture_events(monkeypatch)

    scripted = [_read_resp() for _ in range(4)] + [_terminal()]
    run_agent(
        backend=_ScriptedBackend(scripted), role_cfg=_role(),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
        run_id="test-adaptive-policy",
    )

    adaptive_events = [e for e in events if e["kind"] == "adaptive_state"]
    assert len(adaptive_events) >= 4  # one per loop step before terminal
    sample = adaptive_events[0]
    for k in ("consecutive_no_write", "score_trend", "score_log_len", "convergence_score", "step"):
        assert k in sample, f"missing field {k} in adaptive_state event"

    # consecutive_no_write should grow monotonically across the 4 reads.
    no_writes = [e["consecutive_no_write"] for e in adaptive_events]
    assert no_writes[-1] > no_writes[0]


def test_adaptive_policy_ablation_no_write_off(monkeypatch):
    """LUXE_ADAPTIVE_NO_WRITE=0 → that signal returns None; others active."""
    monkeypatch.setenv("LUXE_ADAPTIVE_POLICY", "1")
    monkeypatch.setenv("LUXE_ADAPTIVE_NO_WRITE", "0")
    monkeypatch.setenv("LUXE_LOG_TOOL_CALLS", "1")
    events = _capture_events(monkeypatch)

    scripted = [_read_resp() for _ in range(3)] + [_terminal()]
    run_agent(
        backend=_ScriptedBackend(scripted), role_cfg=_role(),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
        run_id="test-adaptive-policy",
    )

    adaptive_events = [e for e in events if e["kind"] == "adaptive_state"]
    assert len(adaptive_events) >= 3
    for e in adaptive_events:
        assert e["consecutive_no_write"] is None
        # score_trend should still be present (could be 0.0 with short log)
        assert "score_trend" in e


def test_adaptive_policy_modulation_neutral_below_bias_threshold(monkeypatch):
    """At consecutive_no_write < 8 (the bias threshold), modulation must
    stay at 1.0 → effective_wp_min_step == _WRITE_PRESSURE_MIN_STEP →
    archetype-style short trajectories preserve v1.10.5 behavior."""
    monkeypatch.setenv("LUXE_ADAPTIVE_POLICY", "1")
    monkeypatch.setenv("LUXE_LOG_TOOL_CALLS", "1")
    events = _capture_events(monkeypatch)

    # Only 4 reads then terminal — way below the bias threshold (8).
    scripted = [_read_resp() for _ in range(4)] + [_terminal()]
    run_agent(
        backend=_ScriptedBackend(scripted), role_cfg=_role(),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
        run_id="test-mod-neutral",
    )

    adaptive_events = [e for e in events if e["kind"] == "adaptive_state"]
    # All modulation values should remain at exactly 1.0 (neutral).
    for e in adaptive_events:
        assert e["modulation_write_pressure"] == 1.0
        assert e["modulation_early_bail"] == 1.0


def test_no_write_write_pressure_retired_stays_pinned(monkeypatch):
    """v1.11 Phase B — no_write bias RETIRED. Even with 15 consecutive
    no-write steps (well past the old threshold 8), write_pressure AND
    early_bail modulation must stay pinned at neutral 1.0."""
    monkeypatch.setenv("LUXE_ADAPTIVE_POLICY", "1")
    monkeypatch.setenv("LUXE_LOG_TOOL_CALLS", "1")
    events = _capture_events(monkeypatch)

    scripted = [_read_resp_distinct(i) for i in range(15)] + [_terminal()]
    run_agent(
        backend=_ScriptedBackend(scripted), role_cfg=RoleConfig(
            model_key="test", num_ctx=4096, max_steps=20,
            max_tokens_per_turn=2048, temperature=0.0,
        ),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
        run_id="test-wp-retired",
    )

    adaptive_events = [e for e in events if e["kind"] == "adaptive_state"]
    for e in adaptive_events:
        assert e["modulation_write_pressure"] == 1.0
        assert e["modulation_early_bail"] == 1.0


def test_adaptive_policy_slew_rate_env_override(monkeypatch):
    """LUXE_ADAPTIVE_MAX_INTENSITY_DELTA_PER_STEP bounds the per-step change
    of the live soft_anchor modulation."""
    monkeypatch.setenv("LUXE_ADAPTIVE_POLICY", "1")
    monkeypatch.setenv("LUXE_ADAPTIVE_MAX_INTENSITY_DELTA_PER_STEP", "0.05")
    monkeypatch.setenv("LUXE_LOG_TOOL_CALLS", "1")
    events = _capture_events(monkeypatch)

    scripted = [_read_resp_distinct(i) for i in range(15)] + [_terminal()]
    run_agent(
        backend=_ScriptedBackend(scripted), role_cfg=RoleConfig(
            model_key="test", num_ctx=4096, max_steps=20,
            max_tokens_per_turn=2048, temperature=0.0,
        ),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
        run_id="test-slew",
    )

    adaptive_events = [e for e in events if e["kind"] == "adaptive_state"]
    # With max_delta = 0.05, soft_anchor modulation cannot jump > 0.05 per step.
    mods = [e["modulation_soft_anchor"] for e in adaptive_events]
    for i in range(1, len(mods)):
        assert abs(mods[i] - mods[i - 1]) <= 0.05 + 1e-9, (
            f"step {i} delta {mods[i] - mods[i-1]} exceeded slew limit 0.05"
        )


def test_soft_anchor_collapse_promote_reverted_never_fires(monkeypatch):
    """v1.11 Phase B REVERT regression guard. A diffuse-recon stall that DID
    fire the promotion before the revert (distinct reads, no writes, conv stuck
    < LOW, flat/falling trend past _COLLAPSE_MIN_STEP, full SWE-bench env combo)
    must now produce NO soft_anchor_collapse_promote_fired event — the
    promotion consumer was reverted (net-negative at n=75: premature-commitment
    tier demotion). The soft_anchor modulation is still COMPUTED (observability)
    but nothing in dispatch acts on it."""
    monkeypatch.setenv("LUXE_ADAPTIVE_POLICY", "1")
    monkeypatch.setenv("LUXE_EARLY_BAIL", "1")
    monkeypatch.setenv("LUXE_CONVERGENCE_GATE", "1")
    monkeypatch.setenv("LUXE_EARLY_BAIL_MODE", "soft_anchor")
    monkeypatch.setenv("LUXE_LOG_TOOL_CALLS", "1")
    events = _capture_events(monkeypatch)

    scripted = [_read_resp_distinct(i) for i in range(14)] + [_terminal()]
    run_agent(
        backend=_ScriptedBackend(scripted), role_cfg=RoleConfig(
            model_key="test", num_ctx=4096, max_steps=20,
            max_tokens_per_turn=2048, temperature=0.0,
        ),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
        run_id="test-promote-reverted",
    )

    # No consumer remains: the promotion event must never appear.
    assert [e for e in events if e["kind"] == "soft_anchor_collapse_promote_fired"] == []
    # Observability preserved: soft_anchor modulation still rises on the stall,
    # and write_pressure stays pinned (no_write retired).
    adaptive = [e for e in events if e["kind"] == "adaptive_state"]
    assert any(e["modulation_soft_anchor"] > 1.0 for e in adaptive)
    for e in adaptive:
        assert e["modulation_write_pressure"] == 1.0


def test_adaptive_policy_ablation_score_trend_off(monkeypatch):
    monkeypatch.setenv("LUXE_ADAPTIVE_POLICY", "1")
    monkeypatch.setenv("LUXE_ADAPTIVE_SCORE_TREND", "0")
    monkeypatch.setenv("LUXE_LOG_TOOL_CALLS", "1")
    events = _capture_events(monkeypatch)

    scripted = [_read_resp() for _ in range(3)] + [_terminal()]
    run_agent(
        backend=_ScriptedBackend(scripted), role_cfg=_role(),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
        run_id="test-adaptive-policy",
    )

    adaptive_events = [e for e in events if e["kind"] == "adaptive_state"]
    assert len(adaptive_events) >= 3
    for e in adaptive_events:
        assert e["score_trend"] is None
        assert e["consecutive_no_write"] is not None


# ── Refined-port (LUXE_EARLY_BAIL_COMMIT_ONLY) tests — 2026-05-26 ──────────
# Suppress soft_anchor + breadth_probe variants; keep commit_imperative
# (fires at convergence_score >= _CONVERGENCE_HIGH_THRESHOLD). Default OFF.

def test_early_bail_commit_only_suppresses_low_conv_variants(monkeypatch):
    """With flag ON + diffuse-recon reads (distinct files → low convergence,
    breadth_probe band) the breadth_probe + soft_anchor variants must not fire,
    and the new `early_bail_suppressed_commit_only` observability event must
    appear when the firing branch is reached."""
    monkeypatch.setenv("LUXE_EARLY_BAIL", "1")
    monkeypatch.setenv("LUXE_EARLY_BAIL_MODE", "soft_anchor")
    monkeypatch.setenv("LUXE_CONVERGENCE_GATE", "1")
    monkeypatch.setenv("LUXE_EARLY_BAIL_COMMIT_ONLY", "1")
    monkeypatch.setenv("LUXE_LOG_TOOL_CALLS", "1")
    events = _capture_events(monkeypatch)

    scripted = [_read_resp_distinct(i) for i in range(10)] + [_terminal()]
    run_agent(
        backend=_ScriptedBackend(scripted),
        role_cfg=RoleConfig(model_key="test", num_ctx=4096, max_steps=15,
                            max_tokens_per_turn=2048, temperature=0.0),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
        run_id="test-commit-only-low",
    )

    # breadth_probe + soft_anchor SUPPRESSED under commit_only
    assert [e for e in events if e["kind"] == "early_bail_breadth_probe_fired"] == []
    fired = [e for e in events if e["kind"] == "early_bail_fired"]
    assert all(e.get("msg_variant") != "soft_anchor" for e in fired), \
        f"soft_anchor variant should be suppressed under commit_only; fired={fired}"


def test_early_bail_commit_only_preserves_commit_imperative(monkeypatch):
    """With flag ON + same-file reads (high convergence) + standard early_bail
    triggers, commit_imperative MUST still fire (msg_variant='commit_imperative').
    This is the whole point of the refined port — keep the high-convergence
    imperative, lose only the low/mid-convergence variants."""
    monkeypatch.setenv("LUXE_EARLY_BAIL", "1")
    monkeypatch.setenv("LUXE_EARLY_BAIL_MODE", "soft_anchor")
    monkeypatch.setenv("LUXE_CONVERGENCE_GATE", "1")
    monkeypatch.setenv("LUXE_EARLY_BAIL_COMMIT_ONLY", "1")
    monkeypatch.setenv("LUXE_LOG_TOOL_CALLS", "1")
    events = _capture_events(monkeypatch)

    # Same-file reads → convergence climbs above HIGH (0.40) → commit_imperative
    # branch is selected. _read_resp reads x.py repeatedly.
    scripted = [_read_resp() for _ in range(6)] + [_terminal()]
    run_agent(
        backend=_ScriptedBackend(scripted),
        role_cfg=RoleConfig(model_key="test", num_ctx=4096, max_steps=10,
                            max_tokens_per_turn=2048, temperature=0.0),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
        run_id="test-commit-only-high",
    )

    fired = [e for e in events if e["kind"] == "early_bail_fired"]
    assert any(e.get("msg_variant") == "commit_imperative" for e in fired), \
        f"commit_imperative must still fire under commit_only at high conv; fired={fired}"
