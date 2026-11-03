"""LUXE_IDLE_NARROW (opt-in, 2026-11): what the post-write idle exit counts.

Legacy counts a post-write call idle when it returned no bytes OR any error,
so three verification steps that RAN (a silent `python -c` compile, eslint
exiting 2 with output) end the run with no report
(acceptance/bash_noop_ab_2026_11). Narrow keeps idle only for calls that
returned nothing and did not run. Pins: off is the legacy behavior, each
spare reason, what stays idle, and that identical repeats are still idle
through the dedup short-circuit.
"""

from __future__ import annotations

import pytest

from luxe.agents.flags import RunFlags
from luxe.agents.loop import _narrow_idle_spares, run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.tools.base import ToolCall, ToolDef
from tests._fakes import ScriptedBackend
from tests.test_loop_write_pressure import (
    _make_role, _terminal_resp, _write_resp, _write_tool,
)


def _bash_resp(cmd: str) -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id=cmd, name="bash",
                                     arguments={"command": cmd})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=50),
    )


_BASH_DEF = ToolDef(
    name="bash", description="bash",
    parameters={"type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"]},
)

# command -> (output, error), shaped like tools/shell.py's returns
_RESULTS = {
    "py-compile": ("", None),                          # silent success
    "eslint": ("src/App.jsx: 3 problems\n", "exit code 2"),
    "pytest-fail": ("", "exit code 1"),
    "refused": ("", "Shell chain/redirect operators not allowed: ['&&']."),
    "missing": ("", "exit code 127"),
    "noexec": ("", "exit code 126"),
    "timeout": ("", "Command timed out after 120s"),
}


def _run(monkeypatch, cmds: list[str], narrow: bool):
    if narrow:
        monkeypatch.setenv("LUXE_IDLE_NARROW", "1")
    else:
        monkeypatch.delenv("LUXE_IDLE_NARROW", raising=False)
    events: list[tuple[str, dict]] = []
    import luxe.agents.loop as loop_mod
    monkeypatch.setattr(loop_mod, "append_event",
                        lambda run_id, kind, **f: events.append((kind, f)))
    backend = ScriptedBackend(
        [_write_resp()] + [_bash_resp(c) for c in cmds] + [_terminal_resp()])

    def bash(args):
        return _RESULTS[args["command"].split("#")[0]]

    result = run_agent(
        backend=backend, role_cfg=_make_role(max_steps=30),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_write_tool(), _BASH_DEF],
        tool_fns={"write_file": lambda a: ("ok", None), "bash": bash},
        run_id="idle-narrow-test",
    )
    kinds = [k for k, _ in events]
    return result, kinds, events


def test_flag_is_opt_in():
    assert RunFlags.from_env({}).idle_narrow is False
    assert RunFlags.from_env({"LUXE_IDLE_NARROW": "1"}).idle_narrow is True
    for v in ("", "0", "true", "on"):
        assert RunFlags.from_env({"LUXE_IDLE_NARROW": v}).idle_narrow is False


VERIFY = ["py-compile#a", "eslint#b", "pytest-fail#c", "py-compile#d"]


def test_off_ends_real_verification(monkeypatch):
    """Legacy: three executed checks after a write end the run."""
    _, kinds, _ = _run(monkeypatch, VERIFY, narrow=False)
    assert "post_write_idle_exit" in kinds
    assert "idle_narrow_spared" not in kinds


def test_narrow_lets_executed_checks_run(monkeypatch):
    result, kinds, events = _run(monkeypatch, VERIFY, narrow=True)
    assert "post_write_idle_exit" not in kinds
    assert result.final_text == "done"
    reasons = [f["reason"] for k, f in events if k == "idle_narrow_spared"]
    assert reasons == ["silent_success", "output", "nonzero_exit",
                       "silent_success"]


@pytest.mark.parametrize("cmds", [
    ["refused#a", "missing#b", "timeout#c"],
    ["noexec#a", "refused#b", "missing#c"],
])
def test_narrow_still_exits_on_calls_that_did_not_run(monkeypatch, cmds):
    _, kinds, _ = _run(monkeypatch, cmds + ["py-compile#z"], narrow=True)
    assert "post_write_idle_exit" in kinds
    assert "idle_narrow_spared" not in kinds


def test_narrow_identical_repeat_counts_idle(monkeypatch):
    """A repeat goes through the dedup short-circuit, which counts idle
    whatever the flag says: spared, refused (1), repeat (2), 127 (3)."""
    _, kinds, _ = _run(monkeypatch, ["py-compile#a", "refused#b",
                                     "py-compile#a", "missing#c"], narrow=True)
    assert "post_write_idle_exit" in kinds
    assert kinds.count("idle_narrow_spared") == 1


def test_narrow_back_to_back_repeats_still_end_the_run(monkeypatch):
    """The gate's original target (the same empty check, again and again)
    still ends the run: the stuck-loop guard fires first, as in legacy."""
    result, kinds, _ = _run(monkeypatch, ["py-compile"] * 4, narrow=True)
    assert result.aborted and "repeated same tool calls" in result.abort_reason
    assert kinds.count("idle_narrow_spared") == 1


@pytest.mark.parametrize("out,err,want", [
    ("x", None, "output"),            # unreachable in the loop; legacy not idle
    ("x", "exit code 2", "output"),
    ("", None, "silent_success"),
    ("", "exit code 1", "nonzero_exit"),
    ("", "exit code -9", "nonzero_exit"),
    ("", "exit code 127", None),
    ("", "exit code 126", None),
    ("", "Unknown tool: foo", None),
    ("", "lint checks Python files; use lint_js", None),
    ("", "exit code 1; and more", None),
])
def test_spare_reasons(out, err, want):
    tc = ToolCall(id="", name="bash", arguments={})
    tc.result, tc.error, tc.bytes_out = out, err, len(out.encode())
    assert _narrow_idle_spares(tc) == want
