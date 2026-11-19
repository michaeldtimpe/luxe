"""LUXE_SAVE_MESSAGES: the run's final conversation is RECORDED, never changed.

`run_agent` writes `<run_dir>/messages.jsonl.gz` (gzip JSONL, one message per
line) when the flag is exactly "1", a run_id is set and the session is not
ephemeral. Off = no file and a byte-identical request stream; on = the same
requests plus the file. The writer never raises. Every secret value is FAKE.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path

import pytest

import luxe.agents.loop as loop_mod
import luxe.ephemeral as ephemeral
import luxe.run_state as run_state
import luxe.secrets as secrets
from luxe.agents.loop import run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.config import RoleConfig
from luxe.tools.base import ToolDef

FAKE = "abcdef1234567890-fake"


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("LUXE_SAVE_MESSAGES", raising=False)
    monkeypatch.setattr(secrets, "SECRETS_PATH", tmp_path / "absent.env")
    for k in [k for k in __import__("os").environ if secrets.is_secret_name(k)]:
        monkeypatch.delenv(k)
    secrets.clear_cache()
    ephemeral._reset_for_tests()
    yield
    ephemeral._reset_for_tests()
    secrets.clear_cache()


class _Backend:
    def __init__(self, scripted, final="all done"):
        self._scripted = list(scripted)
        self._final = final
        self.calls: list[str] = []

    def chat(self, messages, **kwargs):
        self.calls.append(json.dumps({"messages": messages, **{
            k: v for k, v in kwargs.items() if k != "on_token"}},
            sort_keys=True, default=str))
        if not self._scripted:
            return ChatResponse(text=self._final, finish_reason="stop",
                                timing=GenerationTiming(prompt_tokens=10,
                                                        completion_tokens=10))
        return self._scripted.pop(0)


def _resp(*calls, text=""):
    return ChatResponse(text=text, tool_calls=list(calls), finish_reason="tool_calls",
                        timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))


_DEF = ToolDef(name="read_file", description="read",
               parameters={"type": "object",
                           "properties": {"path": {"type": "string"}},
                           "required": ["path"]})


def _run(backend, *, run_id="save-test", phase="main", result_text="file body"):
    return run_agent(
        backend=backend,
        role_cfg=RoleConfig(model_key="t", num_ctx=8192, max_steps=6,
                            max_tokens_per_turn=1024, temperature=0.0),
        system_prompt="sys", task_prompt="task", tool_defs=[_DEF],
        tool_fns={"read_file": lambda a: (result_text, None)},
        run_id=run_id, phase=phase)


def _scripted():
    return [_resp(ToolCallResponse(id="c1", name="read_file",
                                   arguments={"path": "a.py"}),
                  text="let me look")]


def _read(p: Path) -> list[dict]:
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f.read().splitlines()]


def _msg_files(tmp_path: Path, run_id="save-test") -> list[Path]:
    d = tmp_path / ".luxe" / "runs" / run_id
    return sorted(d.glob("messages*.jsonl.gz")) if d.is_dir() else []


def test_off_writes_nothing(tmp_path):
    _run(_Backend(_scripted()))
    assert _msg_files(tmp_path) == []


def test_requests_are_byte_identical_on_and_off(monkeypatch, tmp_path):
    off = _Backend(_scripted())
    r_off = _run(off)
    monkeypatch.setenv("LUXE_SAVE_MESSAGES", "1")
    on = _Backend(_scripted())
    r_on = _run(on)
    assert on.calls == off.calls
    assert r_on.messages == r_off.messages
    assert r_on.final_text == r_off.final_text


@pytest.mark.parametrize("value", ["0", "true", "", " 1"])
def test_only_exact_one_enables(monkeypatch, tmp_path, value):
    monkeypatch.setenv("LUXE_SAVE_MESSAGES", value)
    _run(_Backend(_scripted()))
    assert _msg_files(tmp_path) == []


def test_on_writes_the_final_conversation(monkeypatch, tmp_path):
    monkeypatch.setenv("LUXE_SAVE_MESSAGES", "1")
    result = _run(_Backend(_scripted(), final="all done"))
    (p,) = _msg_files(tmp_path)
    assert p.name == "messages.jsonl.gz"
    saved = _read(p)
    assert saved == json.loads(json.dumps(result.messages))
    assert saved[0]["role"] == "system"
    assert saved[-1] == {"role": "assistant", "content": "all done"}
    tool = [m for m in saved if m["role"] == "tool"]
    assert tool and tool[0]["content"] == "file body"


def test_later_passes_do_not_clobber(monkeypatch, tmp_path):
    monkeypatch.setenv("LUXE_SAVE_MESSAGES", "1")
    _run(_Backend(_scripted(), final="first"))
    _run(_Backend(_scripted(), final="second"), phase="reprompt")
    _run(_Backend(_scripted(), final="third"), phase="reprompt")
    names = {p.name: _read(p)[-1]["content"] for p in _msg_files(tmp_path)}
    assert names == {"messages.jsonl.gz": "first",
                     "messages-reprompt.jsonl.gz": "second",
                     "messages-reprompt-2.jsonl.gz": "third"}


def test_no_run_id_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("LUXE_SAVE_MESSAGES", "1")
    _run(_Backend(_scripted()), run_id=None)
    assert not (tmp_path / ".luxe" / "runs").exists() or not list(
        (tmp_path / ".luxe" / "runs").rglob("messages*.jsonl.gz"))


def test_ephemeral_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("LUXE_SAVE_MESSAGES", "1")
    ephemeral.enable()
    _run(_Backend(_scripted()))
    assert _msg_files(tmp_path) == []


def test_writer_failure_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("LUXE_SAVE_MESSAGES", "1")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(gzip, "open", boom)
    result = _run(_Backend(_scripted(), final="ok"))
    assert result.final_text == "ok"
    assert _msg_files(tmp_path) == []


def test_unserializable_message_is_skipped_not_raised(monkeypatch, tmp_path):
    monkeypatch.setattr(run_state, "json", type("J", (), {
        "dumps": staticmethod(lambda *a, **k: (_ for _ in ()).throw(TypeError("x")))}))
    assert run_state.save_messages("r", "main", [{"role": "user"}]) is None


def test_exception_escaping_the_loop_still_saves(monkeypatch, tmp_path):
    monkeypatch.setenv("LUXE_SAVE_MESSAGES", "1")

    def explode(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(loop_mod, "_end_of_step", explode)
    with pytest.raises(KeyboardInterrupt):
        _run(_Backend(_scripted()))
    (p,) = _msg_files(tmp_path)
    assert [m["role"] for m in _read(p)][:2] == ["system", "user"]


def test_a_planted_secret_is_not_persisted(monkeypatch, tmp_path):
    monkeypatch.setenv("LUXE_SAVE_MESSAGES", "1")
    monkeypatch.setenv("FAKE_TEST_TOKEN", FAKE)
    secrets.clear_cache()
    _run(_Backend(_scripted()), result_text=f"OMLX={FAKE}\nother")
    (p,) = _msg_files(tmp_path)
    with gzip.open(p, "rt", encoding="utf-8") as f:
        raw = f.read()
    assert FAKE not in raw
    assert "[REDACTED:FAKE_TEST_TOKEN]" in raw


def test_render_trajectory_shows_saved_messages(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "render_trajectory",
        Path(__file__).parents[1] / "scripts" / "render_trajectory.py")
    rt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rt)

    monkeypatch.setenv("LUXE_SAVE_MESSAGES", "1")
    _run(_Backend(_scripted(), final="all done"), result_text="X" * 1000)
    d = tmp_path / ".luxe" / "runs" / "save-test"
    out = rt.render(d)
    assert "## Conversation (messages.jsonl.gz)" in out
    assert "[PROSE] let me look" in out
    assert '[CALL] read_file({"path": "a.py"})' in out
    assert "[OBSERVATION] read_file: " + "X" * 400 + "…[+600 chars" in out
    assert "[PROSE] all done" in out
    assert "not recorded by luxe" not in out
    assert "messages: " in out


def test_render_trajectory_unchanged_without_saved_messages(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "render_trajectory",
        Path(__file__).parents[1] / "scripts" / "render_trajectory.py")
    rt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rt)
    d = tmp_path / "r"
    d.mkdir()
    out = rt.render(d)
    assert "## Conversation" not in out
    assert out.rstrip().endswith(
        "not recorded by luxe: tool observations and model prose between calls")


# --- bench runners -----------------------------------------------------------

def test_flag_is_not_part_of_the_maintain_suite_fingerprint():
    from benchmarks.maintain_suite.run import config_fingerprint
    base = {"LUXE_WRITE_PRESSURE": "1"}
    fp_off, fields_off = config_fingerprint(None, base)
    fp_on, fields_on = config_fingerprint(None, {**base, "LUXE_SAVE_MESSAGES": "1"})
    assert fp_on == fp_off and fields_on == fields_off
    assert "env:LUXE_SAVE_MESSAGES" not in fields_on


def test_maintain_suite_exports_the_flag_unless_opted_out(monkeypatch, tmp_path):
    import benchmarks.maintain_suite.run as ms
    seen: list[dict] = []

    def capture(cmd, log_dir, env=None, timeout_s=None):
        seen.append(env)
        return 0, "", ""
    monkeypatch.setattr(ms, "_run_capture", capture)
    monkeypatch.delenv("LUXE_SAVE_MESSAGES", raising=False)
    fixture = ms.Fixture(id="f", goal="g", task_type="bugfix", expected_outcome={})

    monkeypatch.setattr(ms, "_SAVE_MESSAGES", True)
    ms._luxe_maintain(tmp_path, fixture, tmp_path)
    assert seen[-1].get("LUXE_SAVE_MESSAGES") == "1"

    monkeypatch.setattr(ms, "_SAVE_MESSAGES", False)
    ms._luxe_maintain(tmp_path, fixture, tmp_path)
    assert "LUXE_SAVE_MESSAGES" not in seen[-1]


def test_swebench_parses_the_luxe_run_id():
    from benchmarks.swebench.adapter import extract_luxe_run_id
    assert extract_luxe_run_id("\nluxe maintain  run_id=0123abcd9f\n") == "0123abcd9f"
    assert extract_luxe_run_id("no banner") == ""
