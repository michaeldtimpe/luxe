"""Tests for chat session resume — transcript turns reconstruct into a live
ChatSession so the next turn's fold carries them forward."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from luxe.chat import resume as resume_mod
from luxe.chat.session import ChatSession
from luxe.memory import session as session_store


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _console():
    return Console(file=io.StringIO(), force_terminal=False, width=100)


def test_resume_missing_returns_false():
    s = ChatSession()
    assert resume_mod.resume_into("nope", s, _console()) is False


def test_resume_reconstructs_turns():
    meta = session_store.new_session(repo_path="/tmp/repo")
    session_store.append_turn(meta.session_id, "user", text="first q", slot="chat")
    session_store.append_turn(meta.session_id, "assistant", text="first a", run_id="r0")
    session_store.append_turn(meta.session_id, "user", text="second q", slot="code")
    session_store.append_turn(meta.session_id, "assistant", text="second a", run_id="r1")

    s = ChatSession()
    ok = resume_mod.resume_into(meta.session_id, s, _console())
    assert ok is True
    assert len(s.turns) == 2
    assert s.turns[0].user == "first q"
    assert s.turns[0].assistant == "first a"
    assert s.turns[1].user == "second q"
    assert s.turns[1].slot == "code"

    # The reconstructed turns feed the next fold.
    ctx, _ = s.build_extra_context("third q")
    assert "first q" in ctx and "second q" in ctx
    assert ctx.rstrip().endswith("</current_request>")


def test_resume_handles_trailing_user_without_assistant():
    meta = session_store.new_session()
    session_store.append_turn(meta.session_id, "user", text="dangling", slot="chat")
    s = ChatSession()
    assert resume_mod.resume_into(meta.session_id, s, _console()) is True
    assert len(s.turns) == 1
    assert s.turns[0].user == "dangling"
    assert s.turns[0].assistant == ""


def test_list_resumable_empty():
    console = _console()
    resume_mod.list_resumable(console)
    assert "No prior chat sessions" in console.file.getvalue()
