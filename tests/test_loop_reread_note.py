"""Re-read note (LUXE_REREAD_NOTE, opt-in, 2026-10-18).

muse-glimmer-30B read all of Game.js 1-600 in its first three reads, then
spent the rest of the run re-reading sub-windows of it (227-256, 288-307,
390-419, 1-100) with fresh arguments each time, and hit max steps
(acceptance/tuning_2026_10). The exact-key repeat-read stub fired once in
six runs. With this switch on, a read whose every line was already returned
since the last write is still served in full, with a note appended.

Default OFF: the loop must be byte-identical with it unset.
"""

from __future__ import annotations

import pytest

from luxe.agents.guardrails import range_is_covered, read_result_line_range
from luxe.agents.loop import run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.config import RoleConfig
from luxe.tools.base import ToolDef

from tests._fakes import ScriptedBackend as _ScriptedBackend

_LINES = [f"line {i}" for i in range(1, 101)]


def _fake_read(args: dict) -> tuple[str, None]:
    off = int(args.get("offset", 0))
    lim = int(args.get("limit", 100))
    rows = _LINES[off:off + lim]
    return "".join(f"{off + i + 1}\t{r}\n" for i, r in enumerate(rows)), None


def _role() -> RoleConfig:
    return RoleConfig(model_key="test", num_ctx=4096, max_steps=30,
                      max_tokens_per_turn=2048, temperature=0.0)


def _resp(name: str, args: dict, call_id: str) -> ChatResponse:
    return ChatResponse(
        text="", tool_calls=[ToolCallResponse(id=call_id, name=name, arguments=args)],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=40))


def _read(call_id: str, path: str = "g.js", **win) -> ChatResponse:
    return _resp("read_file", {"path": path, **win}, call_id)


def _write(call_id: str) -> ChatResponse:
    return _resp("write_file", {"path": "g.js", "content": "x"}, call_id)


def _done() -> ChatResponse:
    return ChatResponse(text="done", finish_reason="stop",
                        timing=GenerationTiming(prompt_tokens=100, completion_tokens=10))


def _tools():
    obj = lambda props: {"type": "object", "properties": props, "required": ["path"]}
    return [
        ToolDef(name="read_file", description="read",
                parameters=obj({"path": {"type": "string"},
                                "offset": {"type": "integer"},
                                "limit": {"type": "integer"}})),
        ToolDef(name="write_file", description="write",
                parameters=obj({"path": {"type": "string"},
                                "content": {"type": "string"}})),
    ]


def _run(script):
    backend = _ScriptedBackend(list(script))
    run_agent(backend=backend, role_cfg=_role(), system_prompt="sys",
              task_prompt="fix the bug", tool_defs=_tools(),
              tool_fns={"read_file": _fake_read,
                        "write_file": lambda a: ("ok", None)})
    return [m["content"] for m in backend.calls[-1] if m.get("role") == "tool"]


def test_line_range_parse():
    assert read_result_line_range("227\ta\n228\tb\n229\tc\n") == (227, 229)
    assert read_result_line_range("File too large: use offset") is None
    assert read_result_line_range("") is None


@pytest.mark.parametrize("seen,lo,hi,want", [
    ([], 1, 5, False),
    ([(1, 50)], 10, 20, True),
    ([(1, 50), (51, 100)], 40, 60, True),       # spans two adjacent reads
    ([(1, 50), (52, 100)], 40, 60, False),      # line 51 never shown
    ([(1, 50)], 40, 60, False),
])
def test_range_is_covered(seen, lo, hi, want):
    assert range_is_covered(seen, lo, hi) is want


def test_default_off_results_are_untouched(monkeypatch):
    monkeypatch.delenv("LUXE_REREAD_NOTE", raising=False)
    out = _run([_read("a", limit=50), _read("b", offset=10, limit=20), _done()])
    assert all("[luxe]" not in c for c in out)


def test_subwindow_of_seen_lines_gets_content_plus_note(monkeypatch):
    monkeypatch.setenv("LUXE_REREAD_NOTE", "1")
    out = _run([_read("a", limit=50), _read("b", offset=50, limit=50),
                _read("c", offset=40, limit=20), _done()])
    assert "[luxe]" not in out[0] and "[luxe]" not in out[1]
    assert out[2].startswith("41\tline 41\n"), "content is still served"
    assert "g.js lines 41-60" in out[2] and "read 3 of g.js" in out[2]


def test_unseen_lines_get_no_note(monkeypatch):
    monkeypatch.setenv("LUXE_REREAD_NOTE", "1")
    out = _run([_read("a", limit=50), _read("b", offset=40, limit=20), _done()])
    assert "[luxe]" not in out[1]


def test_a_write_resets_coverage(monkeypatch):
    monkeypatch.setenv("LUXE_REREAD_NOTE", "1")
    out = _run([_read("a", limit=50), _write("w"),
                _read("b", offset=10, limit=20), _done()])
    assert "[luxe]" not in out[-1], "post-write verification reads stay clean"


def test_other_paths_are_tracked_separately(monkeypatch):
    monkeypatch.setenv("LUXE_REREAD_NOTE", "1")
    out = _run([_read("a", limit=50), _read("b", path="h.js", limit=20), _done()])
    assert "[luxe]" not in out[1]
