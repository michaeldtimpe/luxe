"""Tests for ChatSession context assembly + precedence ordering."""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe.chat.session import (
    CTX_TIERS,
    ChatSession,
    ChatTurn,
    next_tier_up,
    tier_label,
)
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


def test_first_turn_no_memory_write_mode_is_empty_context(repo: Path):
    # Write mode ON + first turn + no memory → legacy byte-identical empty block.
    # `terse` is default-on (B2) and injects a <response_style> block, so the
    # empty-context invariant holds only with terse off.
    s = ChatSession(repo_path=str(repo), write_enabled=True, terse=False)
    ctx, version = s.build_extra_context("what does foo do?")
    assert ctx == ""  # Goal carries the message; nothing else to disambiguate
    assert version == "trunc-v1"


def test_terse_default_injects_response_style(repo: Path):
    # B2: terse defaults on and injects a single high-precedence style block.
    s = ChatSession(repo_path=str(repo), write_enabled=True)
    assert s.terse is True
    ctx, _ = s.build_extra_context("what does foo do?")
    assert "<response_style>" in ctx
    assert "<current_request>" in ctx  # echo restored once anything precedes it


def test_read_only_injects_session_mode_hint_even_on_first_turn(repo: Path):
    # Default (read-only) first turn now carries the /write hint so the model
    # never reports luxe can't create/edit files.
    s = ChatSession(repo_path=str(repo))
    assert s.write_enabled is False
    ctx, _ = s.build_extra_context("scaffold a new project")
    assert "<session_mode>" in ctx
    assert "/write" in ctx
    assert "<current_request>" in ctx  # echo restored once anything precedes it


def test_write_mode_drops_the_session_mode_hint(repo: Path):
    s = ChatSession(repo_path=str(repo), write_enabled=True)
    s.add_turn(ChatTurn(user="hi", assistant="hello"))
    ctx, _ = s.build_extra_context("now what?")
    assert "<session_mode>" not in ctx


def test_session_mode_hint_is_lowest_precedence(repo: Path):
    (repo / ".luxe").mkdir()
    project_mem.repo_memory_file(repo).write_text("Pref: concise.\n")
    s = ChatSession(repo_path=str(repo))  # read-only
    s.add_turn(ChatTurn(user="earlier", assistant="answer"))
    ctx, _ = s.build_extra_context("current ask")
    i_mode = ctx.index("<session_mode>")
    i_mem = ctx.index("<project_memory>")
    i_hist = ctx.index("<conversation_history>")
    i_cur = ctx.index("<current_request>")
    assert i_mode < i_mem < i_hist < i_cur


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


def test_ctx_tier_label_exact_and_custom():
    assert tier_label(CTX_TIERS["medium"]) == "medium"
    assert tier_label(32768) == "medium"
    assert tier_label(40000) == "custom(40000)"


def test_next_tier_up_respects_ceiling():
    # From medium with a 128K ceiling → large is the next step up.
    assert next_tier_up(32768, 131072) == ("large", 65536)
    # From medium with only an 8K ceiling → nothing fits.
    assert next_tier_up(32768, 8192) is None
    # Already at the top tier.
    assert next_tier_up(131072, 131072) is None


def test_num_ctx_override_defaults_off():
    assert ChatSession().num_ctx_override is None


def test_unpromoted_facts_do_not_leak_into_context(repo: Path):
    project_mem.add_fact(repo, "secret auto fact", confidence="auto")
    # terse off so the only thing that could appear is the (forbidden) fact.
    s = ChatSession(repo_path=str(repo), write_enabled=True, terse=False)
    ctx, _ = s.build_extra_context("hello")
    assert "secret auto fact" not in ctx
    assert ctx == ""  # nothing injected
