"""Tests for the chat bottom-toolbar status bar (git-aware, lightweight)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe.chat import status as status_mod
from luxe.chat.session import ChatSession
from luxe.chat.slots import SlotManager
from luxe.chat.status import StatusState, fields, git_status, status_markup
from luxe.config import PipelineConfig, RoleConfig


@pytest.fixture
def slots(monkeypatch):
    from luxe.chat import slots as slots_module

    class FakeBackend:
        def __init__(self, base_url="", model=""):
            self.model = model

        def unload_all_loaded(self, *, except_for=None):
            return {}

    monkeypatch.setattr(slots_module, "Backend", FakeBackend)
    cfg = PipelineConfig(models={"monolith": "Qwen3.6-35B-A3B-6bit"},
                         roles={"monolith": RoleConfig(model_key="monolith")})
    return SlotManager(cfg)


def _text(segs) -> str:
    return " ".join(t for t, _s, _r in segs)


def test_read_only_mode_chip(slots):
    s = ChatSession()
    segs = fields(s, slots, "", StatusState())
    assert "READ-ONLY" in _text(segs)
    assert "WRITE" not in _text(segs)


def test_write_and_bash_chips(slots):
    s = ChatSession(write_enabled=True, unrestricted_bash=True)
    segs = fields(s, slots, "", StatusState())
    txt = _text(segs)
    assert "WRITE" in txt and "BASH" in txt


def test_ctx_tier_shown_when_overridden(slots):
    s = ChatSession(num_ctx_override=131072)
    segs = fields(s, slots, "", StatusState())
    assert "xlarge" in _text(segs)


def test_last_turn_timing_only_after_a_turn(slots):
    s = ChatSession()
    cold = fields(s, slots, "", StatusState())
    assert "tok/s" not in _text(cold)
    warm = fields(s, slots, "", StatusState(
        wall_s=4.0, tok_per_s=50.0, ctx_pressure=0.42, has_turn=True))
    assert "50tok/s" in _text(warm).replace(" ", "") or "tok/s" in _text(warm)


def test_git_status_none_when_not_a_repo(tmp_path: Path):
    assert git_status(str(tmp_path)) is None


def test_git_status_reports_branch_and_dirty(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def g(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, env={**env, "HOME": str(tmp_path)})

    g("init", "-b", "main")
    (repo / "a.txt").write_text("hi")
    g("add", "a.txt")
    g("commit", "-m", "init")
    status_mod._git_cache.clear()
    branch, dirty = git_status(str(repo))
    assert branch == "main" and dirty == 0
    (repo / "b.txt").write_text("new")
    status_mod._git_cache.clear()
    _branch, dirty2 = git_status(str(repo))
    assert dirty2 == 1


def test_git_segment_in_markup(tmp_path: Path, slots, monkeypatch):
    monkeypatch.setattr(status_mod, "git_status", lambda repo: ("feature/x", 3))
    out = status_markup(ChatSession(), slots, "/some/repo", StatusState())
    assert "feature/x" in out and "3" in out
