"""`/ephemeral` — the mid-session toggle.

Distinct from `--ephemeral` in one way that matters: at startup nothing has
been written yet, but mid-session the directory already holds a transcript of
everything said so far. Leaving that in place would be the opposite of what
was asked, so turning it ON purges this session's own state — and says which
paths it removed, because silent deletion of a user's data is worse than not
deleting it.

The line it must not cross: `<repo>/.luxe/memory.md` interleaves luxe's
machine-managed blocks with the user's own curated notes. A mode that writes
nothing must never become a mode that deletes hand-written text.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from luxe import ephemeral
from luxe.chat import commands as cmd
from luxe.chat import slots as slots_mod
from luxe.chat.session import ChatSession
from luxe.chat.status import StatusState
from luxe.config import PipelineConfig, RoleConfig
from luxe.memory import session as session_store
from luxe.run_state import append_event, runs_root


@pytest.fixture(autouse=True)
def clean_flag():
    ephemeral._reset_for_tests()
    yield
    ephemeral._reset_for_tests()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def ctx(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = PipelineConfig(models={"monolith": "Champ"},
                         roles={"monolith": RoleConfig(model_key="monolith")})
    out = io.StringIO()
    session = ChatSession(repo_path=str(repo))
    meta = session_store.new_session(repo_path=str(repo))
    session.session_id = meta.session_id
    c = cmd.CommandContext(
        console=Console(file=out, force_terminal=False, width=200),
        session=session, slots=slots_mod.SlotManager(cfg))
    c._out = out            # type: ignore[attr-defined]
    c._repo = repo          # type: ignore[attr-defined]
    return c


def _text(ctx) -> str:
    return ctx._out.getvalue()


def _run(ctx, *args):
    return cmd.dispatch(f"/ephemeral {' '.join(args)}".strip(), ctx)


class TestItIsRegistered:
    def test_the_command_dispatches(self, ctx):
        res = _run(ctx)
        assert res.handled is True

    def test_it_appears_in_help(self, ctx):
        cmd.dispatch("/help", ctx)
        assert "/ephemeral" in _text(ctx)


class TestToggling:
    def test_bare_invocation_turns_it_on(self, ctx):
        _run(ctx)
        assert ephemeral.is_ephemeral() is True
        assert "ephemeral" in _text(ctx).lower()

    def test_bare_invocation_toggles_back_off(self, ctx):
        _run(ctx)
        _run(ctx)
        assert ephemeral.is_ephemeral() is False

    def test_explicit_on_and_off(self, ctx):
        _run(ctx, "on")
        assert ephemeral.is_ephemeral() is True
        _run(ctx, "off")
        assert ephemeral.is_ephemeral() is False

    def test_a_redundant_call_says_so_and_changes_nothing(self, ctx):
        _run(ctx, "on")
        ctx._out.truncate(0), ctx._out.seek(0)
        _run(ctx, "on")
        assert "already" in _text(ctx).lower()
        assert ephemeral.is_ephemeral() is True

    def test_a_bad_argument_is_rejected_without_toggling(self, ctx):
        _run(ctx, "maybe")
        assert ephemeral.is_ephemeral() is False
        assert "expected on|off" in _text(ctx)


class TestPurgeOnEnable:
    def test_it_removes_this_sessions_state(self, ctx):
        sid = ctx.session.session_id
        session_store.append_turn(sid, "user", text="before the toggle")
        append_event(f"{sid}-0", "tool_call", name="read_file")
        assert session_store.session_dir(sid).exists()

        _run(ctx, "on")

        assert not session_store.session_dir(sid).exists()
        assert not (runs_root() / f"{sid}-0").exists()

    def test_it_names_what_it_deleted(self, ctx):
        """Silent deletion of a user's data is worse than not deleting it."""
        sid = ctx.session.session_id
        session_store.append_turn(sid, "user", text="x")
        _run(ctx, "on")
        out = _text(ctx)
        assert "removed" in out
        assert sid in out

    def test_turning_it_off_purges_nothing(self, ctx):
        sid = ctx.session.session_id
        _run(ctx, "on")
        session_store.new_session(repo_path=str(ctx._repo))
        _run(ctx, "off")
        session_store.append_turn(sid, "user", text="after")
        assert session_store.session_dir(sid).exists()

    def test_curated_project_memory_survives(self, ctx):
        mf = ctx._repo / ".luxe" / "memory.md"
        mf.parent.mkdir(parents=True, exist_ok=True)
        mf.write_text("# hand-written\nkeep me\n")

        _run(ctx, "on")

        assert mf.is_file()
        assert "keep me" in mf.read_text()

    def test_it_discloses_the_memory_file_it_left(self, ctx):
        """Not deleting it is right, but the user must know it is still there
        with their session's notes possibly already in it."""
        mf = ctx._repo / ".luxe" / "memory.md"
        mf.parent.mkdir(parents=True, exist_ok=True)
        mf.write_text("notes\n")
        _run(ctx, "on")
        assert "memory.md" in _text(ctx)

    def test_the_debug_log_handle_is_detached_first(self, ctx):
        """The handler holds debug.log OPEN inside the directory about to be
        removed; detaching after would leave a live handler writing into a
        deleted path."""
        from luxe.chat import debuglog

        sd = session_store.session_dir(ctx.session.session_id)
        sd.mkdir(parents=True, exist_ok=True)
        log = debuglog.install(sd)
        ctx.session_log = log
        assert log.path is not None

        _run(ctx, "on")

        assert log._handler not in __import__("logging").getLogger("luxe").handlers
        assert not sd.exists()


class TestTheStatusChip:
    def test_it_is_absent_when_off(self, ctx):
        from luxe.chat import status as status_mod
        segs = status_mod.fields(ctx.session, ctx.slots, str(ctx._repo), StatusState())
        assert "eph" not in " ".join(status_mod._seg_text(s) for s in segs)

    def test_it_appears_when_on(self, ctx):
        from luxe.chat import status as status_mod
        ephemeral.enable()
        segs = status_mod.fields(ctx.session, ctx.slots, str(ctx._repo), StatusState())
        assert "eph on" in " ".join(status_mod._seg_text(s) for s in segs)

    def test_it_survives_a_narrow_bar(self, ctx):
        """Running without a transcript is not something to hide when the
        terminal gets small — priority 1, like git/ctx/model."""
        from luxe.chat import status as status_mod
        ephemeral.enable()
        segs = status_mod.fields(ctx.session, ctx.slots, str(ctx._repo), StatusState())
        kept = status_mod.fit(segs, 40)
        assert "eph on" in " ".join(status_mod._seg_text(s) for s in kept)


class TestWriteModeIsCompatible:
    """Ephemeral suppresses luxe's OWN bookkeeping, never the model's work."""

    def test_write_mode_can_be_on_at_the_same_time(self, ctx):
        ctx.session.write_enabled = True
        _run(ctx, "on")
        assert ephemeral.is_ephemeral() is True
        assert ctx.session.write_enabled is True

    def test_enabling_write_afterwards_still_works(self, ctx):
        _run(ctx, "on")
        cmd.dispatch("/write", ctx)
        assert ctx.session.write_enabled is True
        assert ephemeral.is_ephemeral() is True

    def test_the_write_tools_are_not_gated_by_it(self, tmp_path):
        """The actual file-writing path must be untouched: `--ephemeral` means
        "luxe records nothing", never "luxe won't edit your repo"."""
        from luxe.tools import fs

        repo = tmp_path / "wrepo"
        repo.mkdir()
        fs.set_repo_root(repo)
        try:
            ephemeral.enable()
            out, err = fs.MUTATION_FNS["write_file"](
                {"path": "made.py", "content": "def f():\n    return 2\n"})
            assert err is None, out
            assert (repo / "made.py").read_text().startswith("def f()")
        finally:
            fs._REPO_ROOT = None
