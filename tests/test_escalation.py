"""Tests for src/luxe/escalation.py — single→swarm context capture."""

from __future__ import annotations

from luxe.escalation import EscalationContext, capture_from_single
from luxe.tools.base import ToolCall


def _tc(name: str, args: dict, result: str = "") -> ToolCall:
    return ToolCall(id="x", name=name, arguments=args, result=result, error=None)


def test_capture_files_read_dedup():
    calls = [
        _tc("read_file", {"path": "src/a.py"}, "..."),
        _tc("read_file", {"path": "src/a.py"}, "..."),  # duplicate
        _tc("read_file", {"path": "src/b.py"}, "..."),
    ]
    ctx = capture_from_single(calls, "plan: do X then Y")
    assert ctx.files_read == ["src/a.py", "src/b.py"]


def test_capture_other_tools_summarized():
    calls = [
        _tc("grep", {"pattern": "TODO"}, "src/a.py:10:TODO refactor"),
        _tc("bash", {"command": "pytest -q"}, "5 passed"),
    ]
    ctx = capture_from_single(calls, "")
    assert any(t[0] == "grep" for t in ctx.tools_invoked)
    assert any(t[0] == "bash" for t in ctx.tools_invoked)


def test_capture_caps_plan_excerpt():
    long_plan = "x" * 5000
    ctx = capture_from_single([], long_plan)
    assert ctx.plan_excerpt.endswith("...")
    assert len(ctx.plan_excerpt) <= 1503  # 1500 + "..."


def test_render_respects_char_cap():
    ctx = EscalationContext(
        files_read=[f"src/file{i}.py" for i in range(100)],
        plan_excerpt="x" * 3000,
        abort_reason="too many edits",
    )
    rendered = ctx.render(char_cap=2000)
    assert len(rendered) <= 2000
    assert rendered.endswith("...")


def test_render_includes_abort_reason():
    ctx = EscalationContext(abort_reason="needs swarm decomposition")
    rendered = ctx.render()
    assert "needs swarm decomposition" in rendered


def test_render_truncates_long_file_lists():
    ctx = EscalationContext(files_read=[f"f{i}.py" for i in range(50)])
    rendered = ctx.render()
    assert "and 20 more" in rendered  # 50 - 30 = 20


def test_render_handles_empty_context():
    ctx = EscalationContext()
    rendered = ctx.render()
    assert "Escalation context" in rendered
    # No abort_reason, no files, no tools, no plan — just the header
    assert len(rendered) < 500
