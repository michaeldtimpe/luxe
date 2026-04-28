"""Tests for src/luxe/agents/single.py — single-mode tool surface assembly.

Full integration tests require a running oMLX backend; these unit tests cover
the deterministic parts: tool surface assembly with allowlist, escalation
signal detection.
"""

from __future__ import annotations

from luxe.agents.loop import AgentResult
from luxe.agents.single import (
    ESCALATE_SIGNAL,
    _build_full_tool_surface,
    did_escalate,
)


def test_full_tool_surface_includes_read_write_shell_git_analysis():
    defs, fns, cacheable = _build_full_tool_surface(
        languages=frozenset({"python"}),
        tool_allowlist=None,
    )
    names = {d.name for d in defs}
    # Read-only fs
    assert {"read_file", "list_dir", "glob", "grep"} <= names
    # Mutation fs
    assert {"write_file", "edit_file"} <= names
    # Git
    assert "git_diff" in names
    # Shell
    assert "bash" in names
    # Analysis (Python lang gates lint/typecheck/etc.)
    assert "lint" in names

    # All names have corresponding fns
    assert names <= set(fns.keys())


def test_allowlist_strips_disallowed_tools():
    defs, fns, _ = _build_full_tool_surface(
        languages=frozenset({"python"}),
        tool_allowlist=["read_file", "grep"],
    )
    names = {d.name for d in defs}
    assert names == {"read_file", "grep"}
    assert set(fns.keys()) == {"read_file", "grep"}


def test_escalation_signal_detected():
    r = AgentResult(final_text=f"Working on this... {ESCALATE_SIGNAL} too many components")
    assert did_escalate(r)


def test_escalation_signal_not_present():
    r = AgentResult(final_text="All done. PR opened.")
    assert not did_escalate(r)


def test_escalation_signal_in_final_only():
    # Empty final_text → no escalation
    r = AgentResult(final_text="")
    assert not did_escalate(r)
