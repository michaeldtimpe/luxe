"""forge-hybrid Phase 3 (B1) — respond terminal tool integration tests.

Covers the LUXE_RESPOND_TERMINAL=1 path:

  - Tool surface inclusion gating (single.py only registers respond when
    the env var is set; verified directly via _build_full_tool_surface).
  - Loop intercept at dispatch site applies 4 watchdog gates BEFORE
    dispatch_tool runs. First match wins; first three inject a reprompt
    and continue; the fourth (terminate) sets result.final_text + breaks
    both inner and outer loops without setting aborted.
  - Wire byte-identity invariant: with LUXE_RESPOND_TERMINAL unset, the
    captured events stream is identical to a flag-on run that doesn't
    actually call respond.

All behavior must be byte-identical to baseline when the env var is
unset — verified by the disabled-default test against the tool surface.
"""

from __future__ import annotations

import os
from typing import Any

import luxe.agents.loop as loop_mod
from luxe.agents.loop import (
    _RESPOND_MIN_STEP,
    run_agent,
)
from luxe.agents.single import _build_full_tool_surface
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.config import RoleConfig
from luxe.tools.base import ToolDef


class _ScriptedBackend:
    """Backend stub that yields a pre-scripted sequence of ChatResponses,
    capturing the messages list passed in on each call so assertions can
    inspect the conversation post-hoc."""

    def __init__(self, scripted: list[ChatResponse]) -> None:
        self._scripted = list(scripted)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, **kwargs) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        if not self._scripted:
            return ChatResponse(
                text="", finish_reason="stop",
                timing=GenerationTiming(prompt_tokens=10, completion_tokens=10),
            )
        return self._scripted.pop(0)


def _make_role(max_steps: int = 30, num_ctx: int = 4096) -> RoleConfig:
    return RoleConfig(model_key="test", num_ctx=num_ctx, max_steps=max_steps,
                      max_tokens_per_turn=2048, temperature=0.0)


def _read_tool() -> ToolDef:
    return ToolDef(
        name="read_file", description="read",
        parameters={"type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"]},
    )


def _write_tool() -> ToolDef:
    return ToolDef(
        name="write_file", description="write",
        parameters={"type": "object",
                    "properties": {"path": {"type": "string"},
                                   "content": {"type": "string"}},
                    "required": ["path", "content"]},
    )


def _edit_tool() -> ToolDef:
    return ToolDef(
        name="edit_file", description="edit",
        parameters={"type": "object",
                    "properties": {"path": {"type": "string"},
                                   "content": {"type": "string"}},
                    "required": ["path", "content"]},
    )


def _respond_tool() -> ToolDef:
    return ToolDef(
        name="respond", description="terminate",
        parameters={"type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"]},
    )


def _read_resp_with_path(path: str, completion_tokens: int = 200) -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="c", name="read_file",
                                     arguments={"path": path})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=completion_tokens),
    )


def _write_resp(path: str = "out.py") -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="w", name="write_file",
                                     arguments={"path": path, "content": "x"})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=200),
    )


def _respond_resp(message: str = "done") -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="r", name="respond",
                                     arguments={"message": message})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=100),
    )


def _terminal_resp() -> ChatResponse:
    return ChatResponse(
        text="done",
        finish_reason="stop",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=100),
    )


def _tool_fns() -> dict[str, Any]:
    return {
        "read_file": lambda args: (f"contents of {args.get('path', '')}", None),
        "write_file": lambda args: ("ok", None),
        "edit_file": lambda args: ("ok", None),
    }


def _capture_events(monkeypatch) -> list[dict]:
    captured: list[dict] = []

    def fake(run_id, kind, **fields):
        captured.append({"kind": kind, **fields})

    monkeypatch.setattr(loop_mod, "append_event", fake)
    return captured


# ---------------------------------------------------------------------------
# Test 1 — Flag OFF: respond is NOT in the surface.
# ---------------------------------------------------------------------------


def test_respond_not_in_surface_when_flag_off(monkeypatch):
    """Without LUXE_RESPOND_TERMINAL=1, single.py's _build_full_tool_surface
    must NOT include `respond` — byte-identical baseline."""
    monkeypatch.delenv("LUXE_RESPOND_TERMINAL", raising=False)
    defs, fns, _ = _build_full_tool_surface(
        languages=None, tool_allowlist=None, task_type="bugfix",
    )
    names = {d.name for d in defs}
    assert "respond" not in names
    assert "respond" not in fns


# ---------------------------------------------------------------------------
# Test 2 — Flag ON: respond IS in the surface.
# ---------------------------------------------------------------------------


def test_respond_in_surface_when_flag_on(monkeypatch):
    """With LUXE_RESPOND_TERMINAL=1, _build_full_tool_surface includes
    respond in both defs and fns."""
    monkeypatch.setenv("LUXE_RESPOND_TERMINAL", "1")
    defs, fns, _ = _build_full_tool_surface(
        languages=None, tool_allowlist=None, task_type="bugfix",
    )
    names = {d.name for d in defs}
    assert "respond" in names
    assert "respond" in fns


# ---------------------------------------------------------------------------
# Test 3 — Clean terminate. writes_seen >= 1, step > last_write_step.
# ---------------------------------------------------------------------------


def test_respond_clean_terminate(monkeypatch):
    """5-step run: writes at step 3, respond at step 4 → final_text set,
    respond_called event fires, loop exits without abort."""
    monkeypatch.setenv("LUXE_RESPOND_TERMINAL", "1")
    monkeypatch.delenv("LUXE_WRITE_PRESSURE", raising=False)
    monkeypatch.delenv("LUXE_EARLY_BAIL", raising=False)
    events = _capture_events(monkeypatch)

    # steps 0..2: reads, step 3: write, step 4: respond.
    scripted = [
        _read_resp_with_path("a.py"),
        _read_resp_with_path("b.py"),
        _read_resp_with_path("c.py"),
        _write_resp("out.py"),
        _respond_resp("did the thing"),
    ]
    backend = _ScriptedBackend(scripted)
    role = _make_role(max_steps=10)

    result = run_agent(
        backend=backend, role_cfg=role,
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool(), _write_tool(), _respond_tool()],
        tool_fns=_tool_fns(),
        run_id="test-clean-terminate",
    )

    assert result.final_text == "did the thing"
    assert result.aborted is False
    called = [e for e in events if e["kind"] == "respond_called"]
    assert len(called) == 1, f"expected exactly 1 respond_called, got {len(called)}"
    assert called[0]["writes_seen"] == 1
    assert called[0]["step"] == 4
    assert called[0]["message_chars"] == len("did the thing")
    # The loop must NOT have continued past respond (no terminal_resp).
    assert len(backend.calls) == 5


# ---------------------------------------------------------------------------
# Test 4 — Early-respond watchdog (writes==0, step < MIN).
# ---------------------------------------------------------------------------


def test_respond_premature_watchdog(monkeypatch):
    """respond at step 2, writes_seen=0 → respond_premature event +
    reprompt; run continues (not terminated)."""
    monkeypatch.setenv("LUXE_RESPOND_TERMINAL", "1")
    monkeypatch.delenv("LUXE_WRITE_PRESSURE", raising=False)
    monkeypatch.delenv("LUXE_EARLY_BAIL", raising=False)
    events = _capture_events(monkeypatch)

    # step 0: read, step 1: read, step 2: respond (premature).
    # After the reprompt the loop continues; we hand back terminal so it ends.
    scripted = [
        _read_resp_with_path("a.py"),
        _read_resp_with_path("b.py"),
        _respond_resp("done"),
        _terminal_resp(),
    ]
    backend = _ScriptedBackend(scripted)
    role = _make_role(max_steps=10)

    result = run_agent(
        backend=backend, role_cfg=role,
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool(), _write_tool(), _respond_tool()],
        tool_fns=_tool_fns(),
        run_id="test-respond-premature",
    )

    premature = [e for e in events if e["kind"] == "respond_premature"]
    assert len(premature) == 1
    assert premature[0]["step"] == 2
    # The run was NOT terminated by respond — final_text comes from the
    # terminal_resp text ("done"), and there was no respond_called event.
    called = [e for e in events if e["kind"] == "respond_called"]
    assert called == []
    # The reprompt landed on the messages list as a _luxe_nudge.
    final = backend.calls[-1]
    nudges = [m for m in final
              if m.get("_luxe_nudge_type") == "respond_premature"]
    assert len(nudges) == 1
    # The step number is substituted into the body.
    assert "after only 2 steps" in nudges[0]["content"]


# ---------------------------------------------------------------------------
# Test 5 — No-writes-late (soft give-up).
# ---------------------------------------------------------------------------


def test_respond_no_writes_late_watchdog(monkeypatch):
    """respond at step 5, writes_seen=0 → respond_no_writes_late event +
    reprompt; run continues."""
    monkeypatch.setenv("LUXE_RESPOND_TERMINAL", "1")
    monkeypatch.delenv("LUXE_WRITE_PRESSURE", raising=False)
    monkeypatch.delenv("LUXE_EARLY_BAIL", raising=False)
    events = _capture_events(monkeypatch)

    # 5 reads then respond. step >= 4 and writes==0 → no_writes_late.
    scripted = (
        [_read_resp_with_path(f"f{i}.py") for i in range(5)]
        + [_respond_resp("done")]
        + [_terminal_resp()]
    )
    backend = _ScriptedBackend(scripted)
    role = _make_role(max_steps=15)

    run_agent(
        backend=backend, role_cfg=role,
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool(), _write_tool(), _respond_tool()],
        tool_fns=_tool_fns(),
        run_id="test-no-writes-late",
    )

    late = [e for e in events if e["kind"] == "respond_no_writes_late"]
    assert len(late) == 1
    assert late[0]["step"] == 5
    called = [e for e in events if e["kind"] == "respond_called"]
    assert called == []
    final = backend.calls[-1]
    nudges = [m for m in final
              if m.get("_luxe_nudge_type") == "respond_no_writes_late"]
    assert len(nudges) == 1
    assert "5 steps" in nudges[0]["content"]


# ---------------------------------------------------------------------------
# Test 6 — Passive surrender (write + respond same step).
# ---------------------------------------------------------------------------


def _write_then_respond_resp() -> ChatResponse:
    """A single backend response carrying BOTH an edit_file call AND a
    respond call. Trips the passive-surrender gate (last_write_step == step
    when respond fires)."""
    return ChatResponse(
        text="",
        tool_calls=[
            ToolCallResponse(id="w", name="edit_file",
                             arguments={"path": "out.py", "content": "x"}),
            ToolCallResponse(id="r", name="respond",
                             arguments={"message": "done"}),
        ],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=200),
    )


def test_respond_passive_surrender_watchdog(monkeypatch):
    """Write + respond emitted in the same step → respond_passive_surrender
    event + reprompt; run continues."""
    monkeypatch.setenv("LUXE_RESPOND_TERMINAL", "1")
    monkeypatch.delenv("LUXE_WRITE_PRESSURE", raising=False)
    monkeypatch.delenv("LUXE_EARLY_BAIL", raising=False)
    events = _capture_events(monkeypatch)

    # Push past the MIN_STEP so the early-respond gate doesn't trip first.
    # 4 reads then a single response that carries edit_file + respond.
    scripted = (
        [_read_resp_with_path(f"f{i}.py") for i in range(4)]
        + [_write_then_respond_resp()]
        + [_terminal_resp()]
    )
    backend = _ScriptedBackend(scripted)
    role = _make_role(max_steps=15)

    run_agent(
        backend=backend, role_cfg=role,
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool(), _edit_tool(), _respond_tool()],
        tool_fns=_tool_fns(),
        run_id="test-passive-surrender",
    )

    surrender = [e for e in events if e["kind"] == "respond_passive_surrender"]
    assert len(surrender) == 1
    # The edit_file landed at step 4; respond fires same step.
    assert surrender[0]["step"] == 4
    assert surrender[0]["last_write_step"] == 4
    called = [e for e in events if e["kind"] == "respond_called"]
    assert called == []
    final = backend.calls[-1]
    nudges = [m for m in final
              if m.get("_luxe_nudge_type") == "respond_passive_surrender"]
    assert len(nudges) == 1
    assert "step 4" in nudges[0]["content"]


# ---------------------------------------------------------------------------
# Test 7 — Compaction × respond (highest priority).
# ---------------------------------------------------------------------------


import pytest


@pytest.mark.skip(reason="compaction x respond integration test deferred: "
                        "requires triggering TieredCompact phase >= 2 fire "
                        "via realistic context-pressure setup; deferred to "
                        "the dedicated compaction integration suite. The "
                        "gate ordering is exercised in "
                        "test_respond_watchdog_ordering_compaction_wins, "
                        "which monkeypatches the phase counter directly.")
def test_respond_compaction_phantom_watchdog(monkeypatch):
    """LUXE_TIERED_COMPACT=1 + LUXE_RESPOND_TERMINAL=1: simulate phase 2
    compaction fire then respond at step 4 with writes_seen=0 →
    respond_compaction_phantom event + reprompt."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Test 8 — Watchdog ordering: compaction-phantom > early-respond.
# ---------------------------------------------------------------------------


def test_respond_watchdog_ordering_compaction_wins(monkeypatch):
    """When both compaction-phantom and early-respond conditions are met,
    the compaction gate fires first (highest priority). We patch the
    loop's compaction_max_phase_this_run state by injecting a fake
    tiered_compactor that bumps phase_reached to 2 on first compact()."""
    monkeypatch.setenv("LUXE_RESPOND_TERMINAL", "1")
    monkeypatch.setenv("LUXE_TIERED_COMPACT", "1")
    monkeypatch.delenv("LUXE_WRITE_PRESSURE", raising=False)
    monkeypatch.delenv("LUXE_EARLY_BAIL", raising=False)
    events = _capture_events(monkeypatch)

    # Patch TieredCompact.compact so phase_reached == 2 from step 0 onward.
    from luxe.context import CompactionResult, TieredCompact

    def fake_compact(self, messages, num_ctx):
        # Phase 2 fire with a fake byte/token delta. Mutates nothing on the
        # messages list (no real compaction needed for the test).
        return CompactionResult(
            messages=messages,
            phase_reached=2,
            tokens_before=1000,
            tokens_after=950,
            tool_results_dropped=1,
        )

    monkeypatch.setattr(TieredCompact, "compact", fake_compact)

    # respond at step 2, writes_seen==0 → would trip early-respond, but
    # phase>=2 + writes==0 takes precedence.
    scripted = [
        _read_resp_with_path("a.py"),
        _read_resp_with_path("b.py"),
        _respond_resp("done"),
        _terminal_resp(),
    ]
    backend = _ScriptedBackend(scripted)
    role = _make_role(max_steps=10)

    run_agent(
        backend=backend, role_cfg=role,
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool(), _write_tool(), _respond_tool()],
        tool_fns=_tool_fns(),
        run_id="test-ordering",
    )

    phantom = [e for e in events if e["kind"] == "respond_compaction_phantom"]
    premature = [e for e in events if e["kind"] == "respond_premature"]
    assert len(phantom) == 1, "compaction-phantom must fire (priority 1)"
    assert premature == [], "early-respond must NOT fire when phantom wins"
    assert phantom[0]["compaction_max_phase"] >= 2
    # Verify the reprompt is the phantom variant.
    final = backend.calls[-1]
    nudges = [m for m in final
              if m.get("_luxe_nudge_type") == "respond_compaction_phantom"]
    assert len(nudges) == 1


# ---------------------------------------------------------------------------
# Test 9 — Wire byte-identity when flag OFF vs flag ON but no respond.
# ---------------------------------------------------------------------------


def _drop_volatile(ev: dict) -> dict:
    """Drop ts/run_id and any fields that vary between runs (none here)."""
    return {k: v for k, v in ev.items() if k not in {"ts", "run_id"}}


def test_respond_flag_off_vs_flag_on_no_call_byte_identical(monkeypatch):
    """Build a captured event sequence for a 4-step run with the flag OFF,
    then again with the flag ON but no respond call in the trajectory.
    The two event streams must match exactly — proves the flag-on path
    is wire-equivalent when respond isn't invoked."""
    # Helper to run one trajectory and capture events.
    def _run_one(flag_on: bool) -> list[dict]:
        if flag_on:
            monkeypatch.setenv("LUXE_RESPOND_TERMINAL", "1")
        else:
            monkeypatch.delenv("LUXE_RESPOND_TERMINAL", raising=False)
        monkeypatch.delenv("LUXE_WRITE_PRESSURE", raising=False)
        monkeypatch.delenv("LUXE_EARLY_BAIL", raising=False)
        captured: list[dict] = []

        def fake(run_id, kind, **fields):
            captured.append({"kind": kind, **fields})

        monkeypatch.setattr(loop_mod, "append_event", fake)

        scripted = [
            _read_resp_with_path("a.py"),
            _read_resp_with_path("b.py"),
            _read_resp_with_path("c.py"),
            _read_resp_with_path("d.py"),
            _terminal_resp(),
        ]
        backend = _ScriptedBackend(scripted)
        role = _make_role(max_steps=10)

        run_agent(
            backend=backend, role_cfg=role,
            system_prompt="sys", task_prompt="do work",
            # Same surface in both runs — caller (single.py) is responsible
            # for gating respond OUT of the surface in the flag-off case.
            # Here we test the LOOP behavior: with respond in the surface
            # but never called, both runs must emit the same event stream.
            tool_defs=[_read_tool(), _write_tool(), _respond_tool()],
            tool_fns=_tool_fns(),
            run_id="test-byte-identity",
        )
        return [_drop_volatile(e) for e in captured]

    off_events = _run_one(flag_on=False)
    on_events = _run_one(flag_on=True)
    assert off_events == on_events, (
        f"event stream drifted between flag-OFF and flag-ON-but-not-fired.\n"
        f"OFF: {off_events}\nON : {on_events}"
    )


# ---------------------------------------------------------------------------
# Test 10 — first_write_step / last_write_step tracking.
# ---------------------------------------------------------------------------


def test_respond_first_and_last_write_step_tracking(monkeypatch):
    """After writes at steps 3, 5, 7 (interleaved with reads), trigger a
    clean respond at step 8 and inspect the respond_called payload —
    last_write_step is implicitly captured via the passive-surrender gate
    not firing (the terminate path requires step > last_write_step).

    The cleaner check: at step 8 with last_write_step=7, the terminate
    path runs and emits respond_called. If last_write_step were stuck at
    3 (the first write), the passive-surrender gate would also pass; but
    a regression where the last_write_step doesn't update would NOT show
    a behavior difference here. So we verify the bookkeeping by
    constructing a passive-surrender scenario: write at step 7, respond
    at step 7 → surrender fires reporting last_write_step=7 (not 3)."""
    monkeypatch.setenv("LUXE_RESPOND_TERMINAL", "1")
    monkeypatch.delenv("LUXE_WRITE_PRESSURE", raising=False)
    monkeypatch.delenv("LUXE_EARLY_BAIL", raising=False)
    events = _capture_events(monkeypatch)

    # Writes at steps 3, 5, 7 (with reads in between), then a same-step
    # write+respond at step 8 to trigger passive surrender — the event
    # payload's last_write_step must equal 8 (the latest), not 3 (first).
    scripted = [
        _read_resp_with_path("a.py"),                  # step 0
        _read_resp_with_path("b.py"),                  # step 1
        _read_resp_with_path("c.py"),                  # step 2
        _write_resp("w1.py"),                          # step 3 — write
        _read_resp_with_path("d.py"),                  # step 4
        _write_resp("w2.py"),                          # step 5 — write
        _read_resp_with_path("e.py"),                  # step 6
        _write_resp("w3.py"),                          # step 7 — write
        _write_then_respond_resp(),                    # step 8 — write+respond → surrender
        _terminal_resp(),
    ]
    backend = _ScriptedBackend(scripted)
    role = _make_role(max_steps=15)

    run_agent(
        backend=backend, role_cfg=role,
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool(), _write_tool(), _edit_tool(), _respond_tool()],
        tool_fns=_tool_fns(),
        run_id="test-tracking",
    )

    surrender = [e for e in events if e["kind"] == "respond_passive_surrender"]
    assert len(surrender) == 1
    # last_write_step in the surrender payload must be the LATEST write
    # step (step 8 from the write+respond pair), not the first (step 3).
    assert surrender[0]["last_write_step"] == 8, (
        f"last_write_step must track most-recent write, got "
        f"{surrender[0]['last_write_step']} (expected 8)"
    )


# ---------------------------------------------------------------------------
# Constants sanity check.
# ---------------------------------------------------------------------------


def test_respond_min_step_constant_sensible():
    """_RESPOND_MIN_STEP should match _EARLY_BAIL_MIN_STEP — both target
    the same "model committed too early without writing" failure shape."""
    from luxe.agents.loop import _EARLY_BAIL_MIN_STEP
    assert _RESPOND_MIN_STEP == 4
    assert _RESPOND_MIN_STEP == _EARLY_BAIL_MIN_STEP
