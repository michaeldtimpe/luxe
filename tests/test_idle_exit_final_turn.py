"""LUXE_IDLE_EXIT_FINAL_TURN (opt-in, 2026-11): one tool-less report turn
after post_write_idle_exit.

The idle gate exits on an acting turn, which carries tool calls and usually
no prose, so the run's report is lost (6/30 control runs in
acceptance/validation_context_ab_2026_11). Pins: off is unchanged (no extra
call), on makes exactly one extra call with NO tools and the fixed prompt,
its text becomes the report, a failure or empty answer keeps what the run
had, and other exits never trigger it.
"""

from __future__ import annotations

from typing import Any

from luxe.agents.loop import run_agent
from luxe.agents.prompts import IDLE_EXIT_FINAL_TURN
from luxe.backend import ChatResponse, GenerationTiming
from tests._fakes import ScriptedBackend
from tests.test_loop_write_pressure import (
    _make_role,
    _terminal_resp,
    _verify_resp,
    _write_resp,
    _write_tool,
    _zero_byte_tool,
)


class _KwBackend(ScriptedBackend):
    """ScriptedBackend that also records each call's kwargs (tools=...)."""

    def __init__(self, scripted, fail_after: int | None = None):
        super().__init__(scripted)
        self.kwargs: list[dict[str, Any]] = []
        self.fail_after = fail_after

    def chat(self, messages, **kwargs):
        self.kwargs.append(kwargs)
        if self.fail_after is not None and len(self.kwargs) > self.fail_after:
            raise RuntimeError("server went away")
        return super().chat(messages, **kwargs)


def _report(text: str) -> ChatResponse:
    return ChatResponse(text=text, finish_reason="stop",
                        timing=GenerationTiming(prompt_tokens=50, completion_tokens=20))


def _idle_run(monkeypatch, *, on: bool, extra: list[ChatResponse] = (),
              fail_after: int | None = None):
    if on:
        monkeypatch.setenv("LUXE_IDLE_EXIT_FINAL_TURN", "1")
    else:
        monkeypatch.delenv("LUXE_IDLE_EXIT_FINAL_TURN", raising=False)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr("luxe.agents.loop.append_event",
                        lambda run_id, kind, **kw: events.append((kind, kw)))
    scripted = [_write_resp(), _verify_resp("lint"), _verify_resp("bash"),
                _verify_resp("typecheck"), *extra]
    backend = _KwBackend(scripted, fail_after=fail_after)
    tools = ["lint", "bash", "typecheck"]
    result = run_agent(
        backend=backend, role_cfg=_make_role(max_steps=30),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_write_tool(), *(_zero_byte_tool(t) for t in tools)],
        tool_fns={"write_file": lambda a: ("ok", None),
                  **{t: (lambda a: ("", None)) for t in tools}},
        run_id="r1",
    )
    return result, backend, events


def test_off_makes_no_extra_call(monkeypatch):
    result, backend, events = _idle_run(monkeypatch, on=False,
                                        extra=[_report("REPORT")])
    assert len(backend.calls) == 4
    assert "idle_exit_final_turn" not in [k for k, _ in events]
    assert result.final_text != "REPORT"


def test_on_makes_one_tool_less_call_and_keeps_its_text(monkeypatch):
    result, backend, events = _idle_run(monkeypatch, on=True,
                                        extra=[_report("Changed X. Ran lint.")])
    assert len(backend.calls) == 5
    assert backend.kwargs[-1]["tools"] is None
    assert all(kw["tools"] for kw in backend.kwargs[:-1])
    assert backend.calls[-1][-1] == {"role": "user", "content": IDLE_EXIT_FINAL_TURN}
    assert result.final_text == "Changed X. Ran lint."
    assert result.aborted is False
    ev = [kw for k, kw in events if k == "idle_exit_final_turn"]
    assert ev and ev[0]["ok"] is True and ev[0]["chars"] == len("Changed X. Ran lint.")


def test_empty_answer_keeps_the_previous_text(monkeypatch):
    result, backend, events = _idle_run(monkeypatch, on=True, extra=[_report("  ")])
    assert len(backend.calls) == 5
    ev = [kw for k, kw in events if k == "idle_exit_final_turn"][0]
    assert ev["ok"] is False and ev["chars"] == 0


def test_backend_failure_is_recorded_not_raised(monkeypatch):
    result, backend, events = _idle_run(monkeypatch, on=True, fail_after=4)
    assert result.aborted is False
    ev = [kw for k, kw in events if k == "idle_exit_final_turn"][0]
    assert ev["ok"] is False and "server went away" in ev["error"]


def test_a_normal_exit_never_triggers_it(monkeypatch):
    monkeypatch.setenv("LUXE_IDLE_EXIT_FINAL_TURN", "1")
    backend = _KwBackend([_write_resp(), _terminal_resp()])
    run_agent(backend=backend, role_cfg=_make_role(max_steps=30),
              system_prompt="sys", task_prompt="do work",
              tool_defs=[_write_tool()],
              tool_fns={"write_file": lambda a: ("ok", None)})
    assert len(backend.calls) == 2


def test_prompt_is_a_positive_imperative():
    assert "Write your final report now" in IDLE_EXIT_FINAL_TURN
    for w in ("rather than", "instead of", "ALWAYS", "NEVER"):
        assert w not in IDLE_EXIT_FINAL_TURN
