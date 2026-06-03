"""Tests for chat slash-command dispatch."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from luxe.chat import commands as cmd
from luxe.chat import slots as slots_mod
from luxe.chat.session import ChatSession
from luxe.config import PipelineConfig, RoleConfig
from luxe.memory import project as project_mem


class FakeBackend:
    def __init__(self, base_url="", model=""):
        self.base_url = base_url
        self.model = model

    def unload_all_loaded(self, *, except_for=None):
        return {}

    def thermal_guard(self, target_model, **kw):
        return True


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    monkeypatch.setattr(slots_mod, "Backend", FakeBackend)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def ctx(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
    )
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=100)
    session = ChatSession(repo_path=str(repo))
    sm = slots_mod.SlotManager(cfg)
    c = cmd.CommandContext(console=console, session=session, slots=sm)
    c._out = out  # type: ignore[attr-defined]
    c._repo = str(repo)  # type: ignore[attr-defined]
    return c


def _text(ctx) -> str:
    return ctx._out.getvalue()


def test_is_command():
    assert cmd.is_command("/help")
    assert not cmd.is_command("hello")


def test_help(ctx):
    res = cmd.dispatch("/help", ctx)
    assert res.handled and not res.exit
    assert "/model" in _text(ctx)


def test_quit_exits(ctx):
    assert cmd.dispatch("/quit", ctx).exit
    assert cmd.dispatch("/exit", ctx).exit


def test_unknown_command(ctx):
    cmd.dispatch("/frobnicate", ctx)
    assert "Unknown command" in _text(ctx)


def test_write_toggles(ctx):
    assert ctx.session.write_enabled is False
    cmd.dispatch("/write", ctx)
    assert ctx.session.write_enabled is True
    cmd.dispatch("/write", ctx)
    assert ctx.session.write_enabled is False


def test_use_pins_slot(ctx):
    cmd.dispatch("/use code", ctx)
    assert ctx.session.pinned_slot == "code"
    cmd.dispatch("/use bogus", ctx)
    assert "Usage" in _text(ctx)


def test_model_list_and_override(ctx):
    cmd.dispatch("/model", ctx)
    assert "chat" in _text(ctx)
    ctx.slots.cfg.models["coder"] = "Coder-Model"
    cmd.dispatch("/model code Coder-Model", ctx)
    assert ctx.slots.model_for("code") == "Coder-Model"


def test_clear_resets_turns(ctx):
    from luxe.chat.session import ChatTurn
    ctx.session.add_turn(ChatTurn(user="hi", assistant="yo"))
    cmd.dispatch("/clear", ctx)
    assert ctx.session.turns == []


def test_memory_add_list_promote_forget(ctx):
    repo = ctx.session.repo_path
    cmd.dispatch("/memory add this repo uses uv", ctx)
    mem = project_mem.load_memory(repo)
    assert len(mem.facts) == 1
    fid = mem.facts[0].id
    assert mem.facts[0].confidence == "manual"  # user-added → injected

    cmd.dispatch("/memory list", ctx)
    assert fid in _text(ctx)

    cmd.dispatch(f"/memory forget {fid}", ctx)
    assert project_mem.load_memory(repo).facts == []


def _ctx_with_ceiling(tmp_path, monkeypatch, num_ctx_max):
    monkeypatch.setattr(slots_mod, "Backend", FakeBackend)
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(
            model_key="monolith", num_ctx=32768, num_ctx_max=num_ctx_max)},
    )
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=120)
    c = cmd.CommandContext(console=console, session=ChatSession(),
                           slots=slots_mod.SlotManager(cfg))
    c._out = out  # type: ignore[attr-defined]
    return c


def test_ctx_show_lists_tiers_and_current(ctx):
    cmd.dispatch("/ctx", ctx)
    out = _text(ctx)
    assert "context window" in out
    assert "small" in out and "xlarge" in out


def test_ctx_set_within_ceiling_no_clamp(tmp_path, monkeypatch):
    c = _ctx_with_ceiling(tmp_path, monkeypatch, num_ctx_max=131072)
    cmd.dispatch("/ctx large", c)
    assert c.session.num_ctx_override == 65536
    assert "clamped" not in c._out.getvalue()


def test_ctx_set_above_ceiling_warns_and_clamps(tmp_path, monkeypatch):
    c = _ctx_with_ceiling(tmp_path, monkeypatch, num_ctx_max=8192)
    cmd.dispatch("/ctx xlarge", c)
    # Stored unclamped; the per-turn apply (repl) clamps to the ceiling.
    assert c.session.num_ctx_override == 131072
    out = c._out.getvalue()
    assert "clamped to 8192" in out


def test_ctx_unknown_tier(ctx):
    cmd.dispatch("/ctx humongous", ctx)
    assert "Unknown size" in _text(ctx)


def test_bash_mode_toggles(ctx):
    assert ctx.session.unrestricted_bash is False
    cmd.dispatch("/bash", ctx)
    assert ctx.session.unrestricted_bash is True
    assert "UNRESTRICTED" in _text(ctx)
    cmd.dispatch("/bash", ctx)
    assert ctx.session.unrestricted_bash is False
    assert "allowlisted" in _text(ctx)


def test_bash_mode_warns_when_read_only(ctx):
    # bash is only exposed in write mode; enabling unrestricted while read-only
    # should hint the user to /write.
    assert ctx.session.write_enabled is False
    cmd.dispatch("/bash", ctx)
    assert "/write" in _text(ctx)


def test_compare_hook_invoked(ctx):
    seen = []
    ctx.on_compare = lambda task: seen.append(task)
    cmd.dispatch("/compare fix the bug", ctx)
    assert seen == ["fix the bug"]


def test_compare_review_hook_invoked(ctx):
    seen = []
    ctx.on_compare_review = lambda cid: seen.append(cid)
    cmd.dispatch("/compare review abc123", ctx)
    assert seen == ["abc123"]


def test_resume_hook_invoked(ctx):
    seen = []
    ctx.on_resume = lambda sid: seen.append(sid)
    cmd.dispatch("/resume xyz", ctx)
    assert seen == ["xyz"]


@pytest.mark.parametrize("alias,kind", [
    ("/gitsummary", "gitsummary"),
    ("/git-summary", "gitsummary"),
    ("/gsum", "gitsummary"),
    ("/gitreview", "gitreview"),
    ("/git-review", "gitreview"),
    ("/grev", "gitreview"),
    ("/gitrefactor", "gitrefactor"),
    ("/git-refactor", "gitrefactor"),
    ("/gref", "gitrefactor"),
])
def test_git_analysis_aliases_dispatch(ctx, alias, kind):
    seen = []
    ctx.on_git_analysis = lambda k: seen.append(k)
    res = cmd.dispatch(alias, ctx)
    assert res.handled and not res.exit
    assert seen == [kind]


def test_git_analysis_no_repo_points_at_cli(ctx):
    ctx.session.repo_path = ""
    seen = []
    ctx.on_git_analysis = lambda k: seen.append(k)
    cmd.dispatch("/gitreview", ctx)
    assert seen == []  # hook NOT called when no repo is bound
    out = _text(ctx)
    assert "luxe gitreview" in out
