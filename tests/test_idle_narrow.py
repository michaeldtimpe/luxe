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
    w = RunFlags.from_env({"LUXE_IDLE_NARROW": "wide"})
    assert w.idle_narrow and w.idle_narrow_wide
    assert not RunFlags.from_env({"LUXE_IDLE_NARROW": "1"}).idle_narrow_wide


def test_wide_spares_refusals():
    assert _narrow_idle_spares(_tc("", "exit code 127"), wide=True) == "refusal"
    assert _narrow_idle_spares(_tc("", "Unknown tool: x"), wide=True) == "refusal"


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


def _tc(out, err):
    tc = ToolCall(id="", name="bash", arguments={})
    tc.result, tc.error, tc.bytes_out = out, err, len(out.encode())
    return tc


def test_wide_does_not_end_on_refusals(monkeypatch):
    monkeypatch.setenv("LUXE_IDLE_NARROW", "wide")
    events = []
    import luxe.agents.loop as loop_mod
    monkeypatch.setattr(loop_mod, "append_event",
                        lambda run_id, kind, **f: events.append((kind, f)))
    backend = ScriptedBackend([_write_resp()] + [_bash_resp(c) for c in
        ["refused#a", "missing#b", "timeout#c", "noexec#d"]] + [_terminal_resp()])
    result = run_agent(
        backend=backend, role_cfg=_make_role(max_steps=30),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_write_tool(), _BASH_DEF],
        tool_fns={"write_file": lambda a: ("ok", None),
                  "bash": lambda a: _RESULTS[a["command"].split("#")[0]]},
        run_id="idle-wide-test",
    )
    kinds = [k for k, _ in events]
    assert "post_write_idle_exit" not in kinds and result.final_text == "done"


# --- LUXE_IDLE_NARROW=capped (2026-11-19): refusals spared up to a budget ---

def test_capped_flag_parsing():
    from luxe.agents.flags import _IDLE_REFUSAL_SPARES_DEFAULT
    c = RunFlags.from_env({"LUXE_IDLE_NARROW": "capped"})
    assert c.idle_narrow and c.idle_narrow_capped and not c.idle_narrow_wide
    assert c.idle_refusal_spares == _IDLE_REFUSAL_SPARES_DEFAULT == 4
    for mode in ("1", "wide", "", "Capped"):
        assert not RunFlags.from_env({"LUXE_IDLE_NARROW": mode}).idle_narrow_capped
    assert not RunFlags.from_env({"LUXE_IDLE_NARROW": "Capped"}).idle_narrow
    env = {"LUXE_IDLE_NARROW": "capped"}
    assert RunFlags.from_env({**env, "LUXE_IDLE_REFUSAL_SPARES": "2"}
                             ).idle_refusal_spares == 2
    assert RunFlags.from_env({**env, "LUXE_IDLE_REFUSAL_SPARES": "0"}
                             ).idle_refusal_spares == 0
    for bad in ("", "x", "-1", "2.5"):
        assert RunFlags.from_env({**env, "LUXE_IDLE_REFUSAL_SPARES": bad}
                                 ).idle_refusal_spares == 4


def _run_mode(monkeypatch, mode, cmds, budget=None):
    monkeypatch.setenv("LUXE_IDLE_NARROW", mode)
    if budget is None:
        monkeypatch.delenv("LUXE_IDLE_REFUSAL_SPARES", raising=False)
    else:
        monkeypatch.setenv("LUXE_IDLE_REFUSAL_SPARES", str(budget))
    events: list[tuple[str, dict]] = []
    import luxe.agents.loop as loop_mod
    monkeypatch.setattr(loop_mod, "append_event",
                        lambda run_id, kind, **f: events.append((kind, f)))
    backend = ScriptedBackend(
        [_write_resp()] + [_bash_resp(c) for c in cmds] + [_terminal_resp()])
    result = run_agent(
        backend=backend, role_cfg=_make_role(max_steps=30),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_write_tool(), _BASH_DEF],
        tool_fns={"write_file": lambda a: ("ok", None),
                  "bash": lambda a: _RESULTS[a["command"].split("#")[0]]},
        run_id="idle-capped-test",
    )
    return result, [k for k, _ in events], events


REFUSALS = ["refused#a", "missing#b", "timeout#c", "noexec#d"]


def test_capped_spares_refusals_under_budget(monkeypatch):
    result, kinds, events = _run_mode(monkeypatch, "capped", REFUSALS)
    assert "post_write_idle_exit" not in kinds and result.final_text == "done"
    assert [f["reason"] for k, f in events if k == "idle_narrow_spared"] \
        == ["refusal"] * 4
    assert "idle_refusal_budget_spent" not in kinds


def test_capped_over_budget_counts_idle_and_exits_at_three(monkeypatch):
    """Budget 1: refusal #1 spared, #2 announces the spent budget and is
    idle (1), #3 idle (2), #4 idle (3) -> the idle exit fires."""
    _, kinds, events = _run_mode(monkeypatch, "capped",
                                 REFUSALS + ["refused#e"], budget=1)
    assert "post_write_idle_exit" in kinds
    assert kinds.count("idle_narrow_spared") == 1
    assert kinds.count("idle_refusal_budget_spent") == 1
    spent = next(f for k, f in events if k == "idle_refusal_budget_spent")
    assert spent["budget"] == 1 and spent["name"] == "bash"
    assert kinds.index("idle_refusal_budget_spent") < kinds.index(
        "post_write_idle_exit")
    exit_ev = next(f for k, f in events if k == "post_write_idle_exit")
    assert exit_ev["idle_tools"] == 3


def test_capped_budget_is_per_run_not_per_streak(monkeypatch):
    """Executed checks reset the idle streak but never refund the budget."""
    cmds = ["refused#a", "py-compile#b", "refused#c", "py-compile#d",
            "missing#e", "timeout#f", "noexec#g"]
    _, kinds, events = _run_mode(monkeypatch, "capped", cmds, budget=2)
    assert "post_write_idle_exit" in kinds
    assert [f["reason"] for k, f in events if k == "idle_narrow_spared"] \
        == ["refusal", "silent_success", "refusal", "silent_success"]
    assert kinds.count("idle_refusal_budget_spent") == 1


def test_capped_keeps_narrow_spares_without_spending_budget(monkeypatch):
    result, kinds, events = _run_mode(monkeypatch, "capped",
                                      VERIFY + ["refused#z"], budget=1)
    assert "post_write_idle_exit" not in kinds and result.final_text == "done"
    assert [f["reason"] for k, f in events if k == "idle_narrow_spared"] \
        == ["silent_success", "output", "nonzero_exit", "silent_success",
            "refusal"]


@pytest.mark.parametrize("mode,exits", [("1", True), ("wide", False)])
def test_other_modes_ignore_the_budget(monkeypatch, mode, exits):
    """narrow and wide behave as before even with a budget set."""
    _, kinds, _ = _run_mode(monkeypatch, mode, REFUSALS, budget=0)
    assert ("post_write_idle_exit" in kinds) is exits
    assert "idle_refusal_budget_spent" not in kinds
    if mode == "1":
        assert "idle_narrow_spared" not in kinds
    else:
        assert kinds.count("idle_narrow_spared") == 4
