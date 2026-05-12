"""Tests for the SpecDD Lever 1 mid-loop reprompt gate in
src/luxe/agents/loop.py — v1.7 priority #2.

The gate threads a Spec parameter through run_agent and reprompts when
agent-trajectory predicates are unsatisfied:
  - expects_zero_calls: reprompts after the first tool call; suppresses
    write_pressure and early_bail (tool-eagerness amplifiers).
  - min_tool_calls: reprompts at loop-break if the model produced fewer
    than min_matches calls; resumes the loop.
"""

from __future__ import annotations

from typing import Any

from luxe.agents.loop import run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.config import RoleConfig
from luxe.spec import Requirement, Spec
from luxe.tools.base import ToolDef


class _ScriptedBackend:
    def __init__(self, scripted: list[ChatResponse]) -> None:
        self._scripted = list(scripted)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, **kwargs) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        if not self._scripted:
            return ChatResponse(text="", finish_reason="stop",
                                timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))
        return self._scripted.pop(0)


def _tool_call_resp(name: str, args: dict[str, Any] | None = None) -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="c", name=name, arguments=args or {})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=50),
    )


def _terminal_resp(text: str = "done") -> ChatResponse:
    return ChatResponse(
        text=text, finish_reason="stop",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=100),
    )


def _role(max_steps: int = 10) -> RoleConfig:
    return RoleConfig(model_key="test", num_ctx=4096, max_steps=max_steps,
                      max_tokens_per_turn=512, temperature=0.0)


def _tool_def(name: str) -> ToolDef:
    return ToolDef(
        name=name, description=name,
        parameters={"type": "object", "properties": {}, "required": []},
    )


def _identity_fn(name: str):
    return {name: lambda args: ("result", None)}


# --- backward compatibility ------------------------------------------------


def test_run_agent_without_spec_works_unchanged():
    """spec=None is the v1.4-v1.6 call shape — must not regress."""
    scripted = [_tool_call_resp("calc"), _terminal_resp()]
    backend = _ScriptedBackend(scripted)
    result = run_agent(
        backend=backend, role_cfg=_role(),
        system_prompt="sys", task_prompt="task",
        tool_defs=[_tool_def("calc")], tool_fns=_identity_fn("calc"),
        spec=None,
    )
    assert result.tool_calls_total == 1
    assert result.final_text == "done"


# --- expects_zero_calls (irrelevance gradient) ----------------------------


def _zero_calls_spec() -> Spec:
    return Spec(goal="abstain", requirements=[Requirement(
        id="R1", must="zero", done_when="zero",
        kind="expects_zero_calls",
    )])


def test_expects_zero_calls_pre_dispatch_blocks_call(monkeypatch):
    """v1.8 Track 2: when spec forbids tool calls and the model emits one,
    the pre-dispatch gate intercepts BEFORE dispatch — the call is dropped
    on the floor (not in actual_tool_calls), and a decline reprompt is
    injected for the model's next turn."""
    scripted = [
        _tool_call_resp("get_weather"),  # violation
        _terminal_resp("I'll decline."),
    ]
    backend = _ScriptedBackend(scripted)
    result = run_agent(
        backend=backend, role_cfg=_role(),
        system_prompt="sys", task_prompt="task",
        tool_defs=[_tool_def("get_weather")], tool_fns=_identity_fn("get_weather"),
        spec=_zero_calls_spec(),
    )
    # The blocked call must NOT appear in tool_calls_total or result.tool_calls.
    assert result.tool_calls_total == 0, (
        f"pre-dispatch gate should have dropped the call; "
        f"got tool_calls_total={result.tool_calls_total}"
    )
    # The second chat (index 1) should see the pre-dispatch reprompt.
    second_chat = backend.calls[1]
    user_msgs = [m for m in second_chat if m.get("role") == "user"]
    assert len(user_msgs) >= 2
    reprompt = user_msgs[-1]["content"]
    assert "not permitted" in reprompt.lower() or "out of scope" in reprompt.lower()


def test_expects_zero_calls_no_reprompt_when_compliant():
    """Model abstains correctly → no reprompt, normal exit."""
    scripted = [_terminal_resp("I cannot answer with these tools.")]
    backend = _ScriptedBackend(scripted)
    result = run_agent(
        backend=backend, role_cfg=_role(),
        system_prompt="sys", task_prompt="task",
        tool_defs=[_tool_def("get_weather")], tool_fns=_identity_fn("get_weather"),
        spec=_zero_calls_spec(),
    )
    assert result.tool_calls_total == 0
    assert result.final_text == "I cannot answer with these tools."
    # Only one chat (no reprompt cycle).
    assert len(backend.calls) == 1


def test_expects_zero_calls_suppresses_early_bail(monkeypatch):
    """When a spec has expects_zero_calls, LUXE_EARLY_BAIL=1 must NOT
    fire even if step/reads thresholds are met. The two interventions
    are mutually contradictory: early_bail pushes the model toward
    action; expects_zero_calls expects inaction."""
    monkeypatch.setenv("LUXE_EARLY_BAIL", "1")

    # 8 read calls + terminal. Without suppression, early_bail would fire
    # at step 4 with 4+ reads. With suppression (zero_calls spec present),
    # it should stay dormant.
    scripted = [_tool_call_resp("read_file", {"path": f"f{i}.py"}) for i in range(8)] \
               + [_terminal_resp()]
    backend = _ScriptedBackend(scripted)
    from luxe.agents.loop import _EARLY_BAIL_MESSAGE
    run_agent(
        backend=backend, role_cfg=_role(),
        system_prompt="sys", task_prompt="task",
        tool_defs=[_tool_def("read_file")], tool_fns=_identity_fn("read_file"),
        spec=_zero_calls_spec(),
    )

    # The early_bail intervention message must never appear.
    for snapshot in backend.calls:
        for msg in snapshot:
            assert _EARLY_BAIL_MESSAGE not in str(msg.get("content", "")), (
                "early_bail fired despite expects_zero_calls suppression"
            )


def test_expects_zero_calls_suppresses_write_pressure(monkeypatch):
    """Same suppression for LUXE_WRITE_PRESSURE — also a tool-eagerness
    amplifier that would corrupt the abstain signal."""
    monkeypatch.setenv("LUXE_WRITE_PRESSURE", "1")
    scripted = [_tool_call_resp("read_file", {"path": f"f{i}.py"}) for i in range(20)] \
               + [_terminal_resp()]
    backend = _ScriptedBackend(scripted)
    from luxe.agents.loop import _WRITE_PRESSURE_MESSAGE
    run_agent(
        backend=backend, role_cfg=_role(max_steps=25),
        system_prompt="sys", task_prompt="task",
        tool_defs=[_tool_def("read_file")], tool_fns=_identity_fn("read_file"),
        spec=_zero_calls_spec(),
    )
    for snapshot in backend.calls:
        for msg in snapshot:
            assert _WRITE_PRESSURE_MESSAGE not in str(msg.get("content", "")), (
                "write_pressure fired despite expects_zero_calls suppression"
            )


def test_expects_zero_calls_pre_dispatch_blocks_every_offending_emission():
    """v1.8 Track 2: unlike the v1.7 post-dispatch fire-once semantics,
    pre-dispatch gating is a capability constraint — every offending
    emission gets blocked. actual_tool_calls must stay empty even after
    multiple violations. The model gets the reprompt repeatedly, which
    is correct: each "I'm trying again" deserves a fresh constraint
    response."""
    scripted = [
        _tool_call_resp("get_weather"),     # blocked 1
        _tool_call_resp("get_weather", {"q": "diff"}),  # blocked 2
        _tool_call_resp("get_weather", {"q": "more"}),  # blocked 3
        _terminal_resp(),
    ]
    backend = _ScriptedBackend(scripted)
    result = run_agent(
        backend=backend, role_cfg=_role(),
        system_prompt="sys", task_prompt="task",
        tool_defs=[_tool_def("get_weather")], tool_fns=_identity_fn("get_weather"),
        spec=_zero_calls_spec(),
    )
    # Zero calls dispatched — pre-dispatch held the line.
    assert result.tool_calls_total == 0
    # Final chat shows multiple decline reprompts in conversation history.
    final_chat = backend.calls[-1]
    declines = [m for m in final_chat
                if m.get("role") == "user"
                and "not permitted" in str(m.get("content", "")).lower()]
    assert len(declines) >= 1, (
        "expected at least one pre-dispatch decline reprompt in history"
    )


# --- min_tool_calls (parallel cliff gradient) ------------------------------


def _min_calls_spec(n: int) -> Spec:
    return Spec(goal=f"need {n}", requirements=[Requirement(
        id="R1", must=f"{n} calls", done_when=f"len >= {n}",
        kind="min_tool_calls", min_matches=n,
    )])


def test_min_tool_calls_reprompts_at_loop_break():
    """Model emits 1 call then terminates; min=3 → reprompt fires and
    the loop continues for at least one more chat."""
    scripted = [
        _tool_call_resp("f1"),
        _terminal_resp("partial answer"),  # model thinks it's done
        _tool_call_resp("f2"),              # after reprompt, model continues
        _tool_call_resp("f3"),
        _terminal_resp("done"),
    ]
    backend = _ScriptedBackend(scripted)
    result = run_agent(
        backend=backend, role_cfg=_role(),
        system_prompt="sys", task_prompt="task",
        tool_defs=[_tool_def("f1"), _tool_def("f2"), _tool_def("f3")],
        tool_fns={"f1": lambda a: ("r", None),
                  "f2": lambda a: ("r", None),
                  "f3": lambda a: ("r", None)},
        spec=_min_calls_spec(3),
    )
    # 3 tool calls landed eventually.
    assert result.tool_calls_total == 3
    # The reprompt must appear somewhere in the messages history.
    found_reprompt = any(
        "Continue" in str(m.get("content", ""))
        for snapshot in backend.calls
        for m in snapshot
        if m.get("role") == "user"
    )
    assert found_reprompt


def test_min_tool_calls_satisfied_breaks_normally():
    """Model emits ≥ min calls before terminating → no reprompt, clean exit."""
    scripted = [
        _tool_call_resp("f1"),
        _tool_call_resp("f2"),
        _terminal_resp("done"),
    ]
    backend = _ScriptedBackend(scripted)
    result = run_agent(
        backend=backend, role_cfg=_role(),
        system_prompt="sys", task_prompt="task",
        tool_defs=[_tool_def("f1"), _tool_def("f2")],
        tool_fns={"f1": lambda a: ("r", None), "f2": lambda a: ("r", None)},
        spec=_min_calls_spec(2),
    )
    assert result.tool_calls_total == 2
    assert result.final_text == "done"
    # No reprompt — only 3 chats (task + 2 tool turns).
    assert len(backend.calls) == 3


def test_min_tool_calls_reprompt_fires_only_once():
    """If model still terminates with too few calls after reprompt, the
    second termination is honored — we don't loop forever."""
    scripted = [
        _tool_call_resp("f1"),
        _terminal_resp("first attempt"),   # 1 call, min=3 → reprompt
        _terminal_resp("second attempt"),  # still 1 call, min=3 → NO reprompt
    ]
    backend = _ScriptedBackend(scripted)
    result = run_agent(
        backend=backend, role_cfg=_role(),
        system_prompt="sys", task_prompt="task",
        tool_defs=[_tool_def("f1")], tool_fns={"f1": lambda a: ("r", None)},
        spec=_min_calls_spec(3),
    )
    # Exit happens despite spec still unsatisfied (fire-once flag holds).
    assert result.tool_calls_total == 1
    assert result.final_text == "second attempt"
