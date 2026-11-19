"""LUXE_FAILED_OUTPUT: a tool that returns BOTH an error and output.

`_dispatch_one` sends `executed.error or executed.result`, so a failing bash
command (`exit code 1` + the pytest output) reached the model as the bare
error line. Off pins that behavior (byte-identical); on sends the error line
followed by the output, through the same redaction and clamp, and records a
`failed_output_shown` event. Every secret value is FAKE.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import luxe.agents.loop as loop_mod
import luxe.ephemeral as ephemeral
import luxe.secrets as secrets
from luxe.agents.flags import RunFlags
from luxe.agents.loop import run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.config import RoleConfig
from luxe.tools.base import ToolDef

FAKE = "abcdef1234567890-fake"
OUT = "FAILED tests/test_x.py::test_y - AssertionError\n1 failed in 0.1s"


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for k in ("LUXE_FAILED_OUTPUT", "LUXE_TOOL_RESULT_CLAMP"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(secrets, "SECRETS_PATH", tmp_path / "absent.env")
    for k in [k for k in __import__("os").environ if secrets.is_secret_name(k)]:
        monkeypatch.delenv(k)
    secrets.clear_cache()
    ephemeral._reset_for_tests()
    yield
    ephemeral._reset_for_tests()
    secrets.clear_cache()


@pytest.fixture
def events(monkeypatch):
    got: list[tuple[str, dict]] = []
    monkeypatch.setattr(loop_mod, "append_event",
                        lambda run_id, kind, **f: got.append((kind, f)))
    return got


class _Backend:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls: list[list[dict]] = []

    def chat(self, messages, **kwargs):
        self.calls.append(json.loads(json.dumps(messages, default=str)))
        if not self._scripted:
            return ChatResponse(text="done", finish_reason="stop",
                                timing=GenerationTiming(prompt_tokens=10,
                                                        completion_tokens=10))
        return self._scripted.pop(0)


_DEF = ToolDef(name="bash", description="bash",
               parameters={"type": "object",
                           "properties": {"command": {"type": "string"}},
                           "required": ["command"]})


def _run(ret, num_ctx=8192):
    backend = _Backend([ChatResponse(
        text="", finish_reason="tool_calls",
        tool_calls=[ToolCallResponse(id="c1", name="bash",
                                     arguments={"command": "pytest"})],
        timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))])
    run_agent(
        backend=backend,
        role_cfg=RoleConfig(model_key="t", num_ctx=num_ctx, max_steps=4,
                            max_tokens_per_turn=1024, temperature=0.0),
        system_prompt="s", task_prompt="t", tool_defs=[_DEF],
        tool_fns={"bash": lambda a: ret}, run_id="fo-test")
    (msg,) = [m for m in backend.calls[-1] if m.get("role") == "tool"]
    return msg["content"], backend.calls


def _shown(events):
    return [f for k, f in events if k == "failed_output_shown"]


def test_flag_parsing():
    assert RunFlags.from_env({}).failed_output is False
    assert RunFlags.from_env({"LUXE_FAILED_OUTPUT": "1"}).failed_output is True
    for v in ("0", "true", "", " 1"):
        assert RunFlags.from_env({"LUXE_FAILED_OUTPUT": v}).failed_output is False


def test_off_sends_the_bare_error(events):
    content, _ = _run((OUT, "exit code 1"))
    assert content == "exit code 1"           # pins the pre-flag behavior
    assert _shown(events) == []


def test_on_sends_error_then_output(monkeypatch, events):
    monkeypatch.setenv("LUXE_FAILED_OUTPUT", "1")
    content, _ = _run((OUT, "exit code 1"))
    assert content == f"exit code 1\n{OUT}"
    (ev,) = _shown(events)
    assert {k: ev[k] for k in ("step", "name", "bytes")} == {
        "step": 0, "name": "bash", "bytes": len(OUT.encode())}


@pytest.mark.parametrize("ret", [("", "exit code 2"), (OUT, None)])
def test_error_only_and_result_only_unchanged(monkeypatch, events, ret):
    off, off_calls = _run(ret)
    monkeypatch.setenv("LUXE_FAILED_OUTPUT", "1")
    on, on_calls = _run(ret)
    assert on == off == (ret[1] or ret[0])
    assert on_calls == off_calls
    assert _shown(events) == []


def test_on_output_is_clamped(monkeypatch, events):
    monkeypatch.setenv("LUXE_FAILED_OUTPUT", "1")
    monkeypatch.setenv("LUXE_TOOL_RESULT_CLAMP", "1")
    big = "\n".join(f"line {i} " + "x" * 80 for i in range(4000))
    content, _ = _run((big, "exit code 1"), num_ctx=8192)
    assert content.startswith("exit code 1\n")
    assert len(content) < len(big)
    assert [k for k, _ in events if k == "tool_result_clamped"]
    assert _shown(events)[0]["bytes"] == len(big.encode())


def test_on_output_is_redacted(monkeypatch, events):
    monkeypatch.setenv("FAKE_TEST_TOKEN", FAKE)
    monkeypatch.setenv("LUXE_FAILED_OUTPUT", "1")
    content, calls = _run((f"env dump TOKEN={FAKE}\n1 failed", "exit code 1"))
    assert content == ("exit code 1\nenv dump "
                       "TOKEN=[REDACTED:FAKE_TEST_TOKEN]\n1 failed")
    assert FAKE not in json.dumps(calls)
    assert FAKE not in json.dumps(events)
