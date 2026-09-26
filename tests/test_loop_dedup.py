"""Duplicate-call detection is invalidated by a successful write (2026-09
review): a `grep`/`bash` repeated AFTER an edit must run again, because the
edit changed what it returns. Before, the loop answered "You already called
… the result was provided above" — false, and it blocked re-verification."""

from __future__ import annotations

from luxe.agents.loop import run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.config import RoleConfig
from luxe.tools.base import ToolDef


class _ScriptedBackend:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append([dict(m) for m in messages])
        if not self._scripted:
            return ChatResponse(text="done", finish_reason="stop",
                                timing=GenerationTiming(prompt_tokens=10,
                                                        completion_tokens=10))
        return self._scripted.pop(0)


def _def(name, *props):
    return ToolDef(name=name, description=name, parameters={
        "type": "object",
        "properties": {p: {"type": "string"} for p in props},
        "required": list(props)})


def _call(i, name, **args):
    return ChatResponse(
        text="", finish_reason="tool_calls",
        tool_calls=[ToolCallResponse(id=f"c{i}", name=name, arguments=args)],
        timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))


def _run(script):
    ran: list[str] = []
    fns = {
        "grep": lambda a: (ran.append("grep") or "hits", None),
        "edit_file": lambda a: (ran.append("edit_file") or "ok", None),
    }
    backend = _ScriptedBackend(script)
    run_agent(backend=backend,
              role_cfg=RoleConfig(model_key="t", num_ctx=8192, max_steps=10,
                                  max_tokens_per_turn=512, temperature=0.0),
              system_prompt="s", task_prompt="t",
              tool_defs=[_def("grep", "pattern"),
                         _def("edit_file", "path", "old_string", "new_string")],
              tool_fns=fns)
    return ran, backend


def test_repeat_before_any_write_is_still_deduplicated():
    ran, _ = _run([_call(1, "grep", pattern="x"), _call(2, "grep", pattern="x")])
    assert ran == ["grep"]


def test_repeat_after_a_write_runs_again():
    ran, backend = _run([
        _call(1, "grep", pattern="x"),
        _call(2, "edit_file", path="a.py", old_string="a", new_string="b"),
        _call(3, "grep", pattern="x"),
    ])
    assert ran == ["grep", "edit_file", "grep"]
    tool_msgs = [m["content"] for m in backend.calls[-1] if m.get("role") == "tool"]
    assert not any(c.startswith("You already called") for c in tool_msgs)


def test_identical_write_repeated_is_still_deduplicated():
    edit = dict(path="a.py", old_string="a", new_string="b")
    ran, _ = _run([_call(1, "edit_file", **edit), _call(2, "edit_file", **edit)])
    assert ran == ["edit_file"]
