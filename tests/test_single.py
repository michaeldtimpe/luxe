"""Tests for src/luxe/agents/single.py — mono-mode tool surface assembly.

Full integration tests require a running oMLX backend; these unit tests cover
the deterministic parts: tool surface assembly with allowlist.
"""

from __future__ import annotations

from luxe.agents.single import _build_full_tool_surface


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
