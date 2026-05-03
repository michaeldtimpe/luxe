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


def test_cve_lookup_gated_to_manage_task_type():
    """cve_lookup must only appear when task_type='manage'.

    On non-audit tasks (implement/document/bugfix/review/None) the surface
    bloat from cve_lookup's tool description deterministically flipped
    lpe-rope-calc-implement-strict-flag from PASS to FAIL in v1.2 (replicated
    3/3 with identical 34913-char prose response). Gating restored 9/10.
    """
    for ttype in (None, "implement", "document", "bugfix", "review"):
        defs, fns, _ = _build_full_tool_surface(
            languages=frozenset({"python"}),
            tool_allowlist=None,
            task_type=ttype,
        )
        names = {d.name for d in defs}
        assert "cve_lookup" not in names, f"cve_lookup leaked into task_type={ttype}"
        assert "cve_lookup" not in fns

    defs, fns, _ = _build_full_tool_surface(
        languages=frozenset({"python"}),
        tool_allowlist=None,
        task_type="manage",
    )
    names = {d.name for d in defs}
    assert "cve_lookup" in names
    assert "cve_lookup" in fns


def test_cve_lookup_gating_respects_allowlist_intersection():
    """Even when task_type=manage, allowlist still applies."""
    defs, _, _ = _build_full_tool_surface(
        languages=frozenset({"python"}),
        tool_allowlist=["read_file"],
        task_type="manage",
    )
    names = {d.name for d in defs}
    assert names == {"read_file"}
