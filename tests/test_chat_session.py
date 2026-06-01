"""Tests for ChatSession context assembly + precedence ordering."""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe.chat.session import ChatSession, ChatTurn
from luxe.memory import project as project_mem


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    return r


def test_first_turn_no_memory_is_empty_context(repo: Path):
    s = ChatSession(repo_path=str(repo))
    ctx, version = s.build_extra_context("what does foo do?")
    assert ctx == ""  # Goal carries the message; nothing else to disambiguate
    assert version == "trunc-v1"


def test_memory_only_injects_project_block_and_echo(repo: Path):
    (repo / ".luxe").mkdir()
    project_mem.repo_memory_file(repo).write_text("Use ruff.\n")
    s = ChatSession(repo_path=str(repo))
    ctx, _ = s.build_extra_context("add a test")
    assert "<project_memory>" in ctx
    assert "Use ruff." in ctx
    assert "<current_request>" in ctx
    assert "add a test" in ctx
    assert "<conversation_history>" not in ctx


def test_history_injects_conversation_block_and_echo(repo: Path):
    s = ChatSession(repo_path=str(repo))
    s.add_turn(ChatTurn(user="hi", assistant="hello"))
    ctx, _ = s.build_extra_context("now what?")
    assert "<conversation_history>" in ctx
    assert "[user] hi" in ctx
    assert "<current_request>" in ctx
    assert "now what?" in ctx


def test_precedence_order_memory_then_history_then_current(repo: Path):
    (repo / ".luxe").mkdir()
    project_mem.repo_memory_file(repo).write_text("Pref: concise.\n")
    s = ChatSession(repo_path=str(repo))
    s.add_turn(ChatTurn(user="earlier question", assistant="earlier answer"))
    ctx, _ = s.build_extra_context("current ask")
    i_mem = ctx.index("<project_memory>")
    i_hist = ctx.index("<conversation_history>")
    i_cur = ctx.index("<current_request>")
    assert i_mem < i_hist < i_cur  # documented precedence ordering
    # current request is the LAST-seen content
    assert ctx.rstrip().endswith("</current_request>")


def test_unpromoted_facts_do_not_leak_into_context(repo: Path):
    project_mem.add_fact(repo, "secret auto fact", confidence="auto")
    s = ChatSession(repo_path=str(repo))
    ctx, _ = s.build_extra_context("hello")
    assert "secret auto fact" not in ctx
    assert ctx == ""  # nothing injected
