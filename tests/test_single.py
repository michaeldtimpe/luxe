"""Tests for src/luxe/agents/single.py — mono-mode tool surface assembly.

Full integration tests require a running oMLX backend; these unit tests cover
the deterministic parts: tool surface assembly with allowlist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe.agents import single as single_mod
from luxe.agents.loop import AgentResult
from luxe.agents.single import _build_full_tool_surface, _build_sdd_block, run_single
from luxe.config import RoleConfig
from luxe.tools import fs


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


# --- SpecDD Lever 2: prompt-side .sdd injection ---------------------------


class TestSddBlockInjection:
    def test_no_repo_root_returns_empty(self):
        # Defensive — _build_sdd_block must not raise when fs hasn't been
        # configured yet (test environments, dry-run prompt construction).
        fs._REPO_ROOT = None
        assert _build_sdd_block() == ""

    def test_no_sdd_files_returns_empty(self, tmp_path: Path):
        fs.set_repo_root(tmp_path)
        try:
            assert _build_sdd_block() == ""
        finally:
            fs._REPO_ROOT = None

    def test_renders_sdd_block_for_real_contracts(self, tmp_path: Path):
        sdd_dir = tmp_path / "src" / "luxe"
        sdd_dir.mkdir(parents=True)
        (sdd_dir / "luxe.sdd").write_text(
            "# luxe\n## Forbids\n- tests/**\n",
            encoding="utf-8",
        )
        fs.set_repo_root(tmp_path)
        try:
            block = _build_sdd_block()
            assert block.startswith("\n\n")  # detached from preceding text
            assert "## Repository contracts" in block
            assert "Forbids: tests/**" in block
            assert "src/luxe/luxe.sdd" in block
        finally:
            fs._REPO_ROOT = None

    def test_malformed_sdd_does_not_crash_prompt_construction(self, tmp_path: Path):
        # Tool-side check surfaces the malformed-sdd error on first
        # write attempt; prompt construction must not crash beforehand.
        sdd_path = tmp_path / "broken" / "broken.sdd"
        sdd_path.parent.mkdir()
        sdd_path.write_text("## Must\n- a\n## Must\n- b\n", encoding="utf-8")
        fs.set_repo_root(tmp_path)
        try:
            assert _build_sdd_block() == ""
        finally:
            fs._REPO_ROOT = None


# --- extra_context injection seam (chat front-end) ------------------------


class TestExtraContextSeam:
    """run_single's `extra_context` is the single chat-injection seam. The
    default ("") must produce a task_prompt byte-identical to legacy callers;
    a non-empty block is appended verbatim after the .sdd block."""

    def _capture_task_prompt(self, monkeypatch, **kwargs) -> str:
        captured: dict[str, str] = {}

        def fake_run_agent(backend, role_cfg, *, task_prompt, **_):
            captured["task_prompt"] = task_prompt
            return AgentResult()

        monkeypatch.setattr(single_mod, "run_agent", fake_run_agent)
        role = RoleConfig(model_key="monolith", tools=["read_file"])
        run_single(
            backend=object(),
            role_cfg=role,
            goal="do the thing",
            task_type="review",
            languages=frozenset({"python"}),
            **kwargs,
        )
        return captured["task_prompt"]

    def test_default_extra_context_is_byte_identical(self, monkeypatch):
        # No repo root => no .sdd block; isolates the seam.
        fs._REPO_ROOT = None
        without = self._capture_task_prompt(monkeypatch)
        explicit_empty = self._capture_task_prompt(monkeypatch, extra_context="")
        assert without == explicit_empty
        # Legacy shape preserved exactly.
        assert without.startswith("Task type: review\nGoal: do the thing\n\n")
        assert "<conversation_history>" not in without

    def test_non_empty_extra_context_appended_verbatim(self, monkeypatch):
        fs._REPO_ROOT = None
        base = self._capture_task_prompt(monkeypatch)
        block = "\n\n<project_memory>\nuse ruff\n</project_memory>"
        with_ctx = self._capture_task_prompt(monkeypatch, extra_context=block)
        assert with_ctx == base + block
