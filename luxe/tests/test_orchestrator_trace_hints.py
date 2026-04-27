"""Tests for the orchestrator's _augment_with_trace_hints.

Exercises the pre-retrieval path that reads files cited in pasted
tracebacks before dispatching the subtask to an agent.
"""

from __future__ import annotations

from pathlib import Path

from luxe_cli.tasks.model import Subtask, Task
from luxe_cli.tasks.orchestrator import _augment_with_trace_hints


def _sub(title: str, index: int = 0) -> Subtask:
    return Subtask(id=f"s{index}", parent_id="t", index=index, title=title)


def _task(subs: list[Subtask]) -> Task:
    return Task(id="t", goal="test", subtasks=subs)


def test_empty_when_no_trace_paths(tmp_path):
    sub = _sub("Just write a haiku about autumn")
    task = _task([sub])
    assert _augment_with_trace_hints(task, sub, tmp_path) == ""


def test_inlines_cited_file_contents(tmp_path):
    body = "def handle_request():\n    return 'ok'\n"
    (tmp_path / "app").mkdir()
    (tmp_path / "app/handlers.py").write_text(body)
    sub = _sub(
        'fix the bug, pytest output:\n'
        '  File "app/handlers.py", line 2, in handle_request\n'
        '    assert False'
    )
    task = _task([sub])

    out = _augment_with_trace_hints(task, sub, tmp_path)
    assert "# Files mentioned in the error you're debugging" in out
    assert "## app/handlers.py" in out
    assert body in out


def test_truncates_long_files(tmp_path):
    big = "\n".join(f"line_{i}" for i in range(500))
    (tmp_path / "big.py").write_text(big)
    sub = _sub("see big.py:42 for the problem")
    task = _task([sub])

    out = _augment_with_trace_hints(task, sub, tmp_path, max_lines=50)
    assert "## big.py (first 50 of 500 lines)" in out
    assert "line_49" in out       # included
    assert "line_100" not in out  # trimmed


def test_pulls_trace_from_prior_subtask_results(tmp_path):
    """A trace pasted in subtask 0's result_text should still seed
    pre-retrieval for subtask 1."""
    (tmp_path / "bug.py").write_text("x = 1\n")
    s0 = Subtask(
        id="s0", parent_id="t", index=0,
        title="collect error output",
        status="done",
        result_text='Traceback:\n  File "bug.py", line 1, in <module>',
    )
    s1 = _sub("fix the bug", index=1)
    task = _task([s0, s1])

    out = _augment_with_trace_hints(task, s1, tmp_path)
    assert "## bug.py" in out


def test_honours_max_files_cap(tmp_path):
    for name in ("a.py", "b.py", "c.py", "d.py"):
        (tmp_path / name).write_text(f"# {name}\n")
    sub = _sub("errors in a.py:1, b.py:2, c.py:3, d.py:4")
    task = _task([sub])

    out = _augment_with_trace_hints(task, sub, tmp_path, max_files=2)
    # Only first two survive.
    assert "## a.py" in out
    assert "## b.py" in out
    assert "## c.py" not in out
    assert "## d.py" not in out
