"""Repeat-read stub (LUXE_REPEAT_READ_STUB, opt-in, 2026-10-17).

muse-glimmer-30B re-read the same 30 lines of Game.js at steps 19, 24 and 29
of a run it had already solved in its reasoning, and ran out of steps
(acceptance/tuning_2026_10). `read_file` is dedup-exempt, so each repeat
returned the full text again. With the switch on, a repeat that is still
current — no write since, earlier result intact in context — gets a short
pointer back to that result instead.

Default OFF: the loop must be byte-identical with it unset.
"""

from __future__ import annotations

from luxe.agents.loop import run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.config import RoleConfig
from luxe.tools.base import ToolDef

from tests._fakes import ScriptedBackend as _ScriptedBackend

_BODY = "def add(a, b):\n    return a + b\n"


def _role() -> RoleConfig:
    return RoleConfig(model_key="test", num_ctx=4096, max_steps=30,
                      max_tokens_per_turn=2048, temperature=0.0)


def _resp(name: str, args: dict, call_id: str) -> ChatResponse:
    return ChatResponse(
        text="", tool_calls=[ToolCallResponse(id=call_id, name=name, arguments=args)],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=40))


def _read(call_id: str, path: str = "calc.py") -> ChatResponse:
    return _resp("read_file", {"path": path}, call_id)


def _write(call_id: str) -> ChatResponse:
    return _resp("write_file", {"path": "calc.py", "content": "x"}, call_id)


def _done() -> ChatResponse:
    return ChatResponse(text="done", finish_reason="stop",
                        timing=GenerationTiming(prompt_tokens=100, completion_tokens=10))


def _tools():
    obj = lambda props: {"type": "object", "properties": props,
                         "required": list(props)}
    return [
        ToolDef(name="read_file", description="read",
                parameters=obj({"path": {"type": "string"}})),
        ToolDef(name="write_file", description="write",
                parameters=obj({"path": {"type": "string"},
                                "content": {"type": "string"}})),
    ]


def _run(script):
    reads: list[dict] = []
    backend = _ScriptedBackend(list(script))
    result = run_agent(
        backend=backend, role_cfg=_role(), system_prompt="sys",
        task_prompt="fix the bug", tool_defs=_tools(),
        tool_fns={
            "read_file": lambda a: (reads.append(a) or _BODY, None),
            "write_file": lambda a: ("ok", None),
        },
    )
    return backend, result, reads


def _tool_results(backend) -> list[str]:
    return [m["content"] for m in backend.calls[-1] if m.get("role") == "tool"]


def test_default_off_repeat_reads_return_full_content(monkeypatch):
    monkeypatch.delenv("LUXE_REPEAT_READ_STUB", raising=False)
    backend, _, reads = _run([_read("r1"), _read("r2"), _done()])
    assert len(reads) == 2
    assert _tool_results(backend) == [_BODY, _BODY]


def test_enabled_unchanged_repeat_gets_the_stub(monkeypatch):
    monkeypatch.setenv("LUXE_REPEAT_READ_STUB", "1")
    backend, result, reads = _run([_read("r1"), _read("r2"), _done()])
    assert len(reads) == 1, "the repeat must not re-run the tool"
    first, second = _tool_results(backend)
    assert first == _BODY
    assert "already read calc.py" in second and _BODY not in second
    assert result.tool_calls[-1].duplicate is True


def test_enabled_a_write_in_between_returns_fresh_content(monkeypatch):
    monkeypatch.setenv("LUXE_REPEAT_READ_STUB", "1")
    backend, _, reads = _run([_read("r1"), _write("w"), _read("r2"), _done()])
    assert len(reads) == 2, "post-write verification reads must run"
    assert _tool_results(backend)[-1] == _BODY


def test_enabled_different_arguments_are_not_a_repeat(monkeypatch):
    monkeypatch.setenv("LUXE_REPEAT_READ_STUB", "1")
    _, _, reads = _run([_read("r1"), _read("r2", path="other.py"), _done()])
    assert len(reads) == 2


def test_enabled_a_compacted_earlier_result_is_read_again(monkeypatch):
    """If the earlier result no longer sits in context as sent, the stub would
    point at nothing — the read must run."""
    monkeypatch.setenv("LUXE_REPEAT_READ_STUB", "1")
    import luxe.agents.loop as loop_mod

    real = loop_mod.repeat_read_stub_message
    seen: list[str] = []

    def spy(path):
        seen.append(path)
        return real(path)

    monkeypatch.setattr(loop_mod, "repeat_read_stub_message", spy)

    class Mutating(_ScriptedBackend):
        def chat(self, messages, **kw):
            # Simulate compaction rewriting the first tool result in place.
            for m in messages:
                if m.get("role") == "tool" and m.get("content") == _BODY:
                    m["content"] = "[Truncated]"
            return super().chat(messages, **kw)

    reads: list[dict] = []
    backend = Mutating([_read("r1"), _read("r2"), _done()])
    run_agent(backend=backend, role_cfg=_role(), system_prompt="sys",
              task_prompt="fix the bug", tool_defs=_tools(),
              tool_fns={"read_file": lambda a: (reads.append(a) or _BODY, None),
                        "write_file": lambda a: ("ok", None)})
    assert len(reads) == 2 and seen == []
