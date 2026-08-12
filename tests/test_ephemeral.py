"""Ephemeral mode — a session that leaves nothing behind.

`--ephemeral` suppresses every one of luxe's own persistence sites: the
`~/.luxe/sessions/<id>/` tree (meta, transcript, fold, debug.log),
`~/.luxe/runs/<id>/events.jsonl`, and the repo's `.luxe/memory.md` +
`facts.jsonl`.

The tests that matter most here are the NEGATIVE ones — that nothing appears
under a temp `$HOME` after exercising every writer — and the boundary ones,
because the name invites a wider reading than the feature has:

  - reads are untouched (config, secrets, existing project memory);
  - the write TOOLS are unaffected — `/write` still gates those, and
    "ephemeral" never means "won't touch your repo";
  - the repo lock is still taken, deliberately, and is the one artifact that
    survives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from luxe import ephemeral
from luxe.memory import project as project_mem
from luxe.memory import session as session_store
from luxe.run_state import append_event, run_dir


@pytest.fixture(autouse=True)
def clean_flag():
    ephemeral._reset_for_tests()
    yield
    ephemeral._reset_for_tests()


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    return h


def _exercise_every_writer(repo: Path) -> str:
    """Drive each persistence site once. Returns the session id."""
    meta = session_store.new_session(repo_path=str(repo), project_hash="h")
    session_store.append_turn(meta.session_id, "user", text="hello")
    session_store.append_fold(meta.session_id, 0, "v1", "folded")
    session_store.touch(meta.session_id)
    append_event("run-1", "tool_call", name="read_file")
    project_mem.splice_block(repo, "notes", "a note")
    return meta.session_id


class TestOff:
    """The default. Everything lands, exactly as before."""

    def test_every_writer_produces_its_file(self, home, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        sid = _exercise_every_writer(repo)

        d = session_store.session_dir(sid)
        assert (d / "meta.json").is_file()
        assert (d / "transcript.jsonl").is_file()
        assert (d / "fold.jsonl").is_file()
        assert (run_dir("run-1") / "events.jsonl").is_file()
        assert (repo / ".luxe" / "memory.md").is_file()


class TestOn:
    def test_nothing_is_written_anywhere(self, home, tmp_path):
        """The headline guarantee, checked by walking the whole of $HOME
        rather than by naming the files we happen to know about — a writer
        added later fails this without anyone remembering to update it."""
        repo = tmp_path / "repo"
        repo.mkdir()
        ephemeral.enable()
        _exercise_every_writer(repo)

        strays = [p for p in home.rglob("*") if p.is_file()]
        assert strays == [], f"ephemeral session wrote: {strays}"
        assert not (repo / ".luxe").exists()

    def test_a_session_id_is_still_issued(self, home, tmp_path):
        """The id keys the in-memory session and every log line; only the
        directory goes away."""
        ephemeral.enable()
        meta = session_store.new_session(repo_path=str(tmp_path))
        assert meta.session_id
        assert not session_store.session_dir(meta.session_id).exists()

    def test_the_writers_stay_callable_and_silent(self, home, tmp_path):
        """Every suppressed writer is called unconditionally from paths that
        must not learn about this mode. They must no-op, not raise."""
        ephemeral.enable()
        session_store.append_turn("nosuch", "user", text="x")
        session_store.append_fold("nosuch", 0, "v1", "x")
        session_store.touch("nosuch")
        append_event("run-x", "tool_call", name="read_file")

    def test_a_debug_log_is_not_installed(self, home, tmp_path):
        from luxe.chat import debuglog
        ephemeral.enable()
        log = debuglog.install(tmp_path)
        assert log.path is None
        debuglog.uninstall(log)          # must stay safe to call
        assert not (tmp_path / "debug.log").exists()

    def test_splice_block_returns_the_path_without_writing(self, home, tmp_path):
        """Callers use the return value to report where a note landed; it has
        to stay a Path, not None."""
        ephemeral.enable()
        got = project_mem.splice_block(tmp_path, "notes", "body")
        assert isinstance(got, Path)
        assert not got.exists()


class TestTheOtherWriters:
    """Sites found 2026-08-11 by asking "what else makes files?".

    The first pass covered the session tree, run events and project memory —
    and missed four more, two of which create `.luxe` directories outright.
    `update_ledger` is the sharpest: it is exposed to the model on EVERY chat
    turn (repl.py), so any session could recreate the session directory the
    mode had just declined to make.
    """

    def test_the_ledger_does_not_recreate_the_session_dir(self, home, tmp_path):
        from luxe.state import ledger as ledger_mod

        ephemeral.enable()
        ledger_mod.record_files("sess1", ["a.py"])
        assert not session_store.session_dir("sess1").exists()

    def test_gitkit_does_not_mirror_into_the_repo(self, home, tmp_path):
        """`mirror_to_repo` literally creates `<repo>/.luxe/gitkit/` — the
        exact thing the mode promises not to do. Reachable from chat via
        /gitaudit and /gitchange."""
        from luxe.gitkit import store as gitkit_store

        repo = tmp_path / "repo"
        repo.mkdir()
        ephemeral.enable()
        assert gitkit_store.mirror_to_repo(repo, "gitaudit", "# report", "head") is None
        assert not (repo / ".luxe").exists()

    def test_gitkit_does_not_file_the_report(self, home, tmp_path):
        """The report is still returned and rendered — just never written."""
        from luxe.gitkit import store as gitkit_store

        repo = tmp_path / "repo"
        repo.mkdir()
        ephemeral.enable()
        assert gitkit_store.save_report(repo, "gitaudit", "# report") is None
        assert not (home / ".luxe" / "reports").exists()

    def test_the_cve_cache_is_not_written(self, home, tmp_path):
        from luxe.tools import cve_lookup

        ephemeral.enable()
        cve_lookup._write_cache(tmp_path / "x.json", {"id": "CVE-1"})
        assert not (tmp_path / "x.json").exists()

    def test_the_mcp_audit_log_is_not_written(self, home):
        from luxe.mcp import server as mcp_server

        ephemeral.enable()
        mcp_server.append_audit("luxe_review", {}, "ok")
        assert not mcp_server.audit_log_path().exists()


class TestBoundaries:
    def test_existing_project_memory_is_still_readable(self, home, tmp_path):
        """Suppressing WRITES must not blind the session: a repo with curated
        memory should still inject it."""
        repo = tmp_path / "repo"
        (repo / ".luxe").mkdir(parents=True)
        (repo / ".luxe" / "memory.md").write_text("curated fact\n")
        ephemeral.enable()
        assert "curated fact" in project_mem.load_memory(repo).curated_md

    def test_curated_memory_is_not_destroyed(self, home, tmp_path):
        """The nightmare case: a mode meant to write nothing truncating a
        user's file on the way past."""
        repo = tmp_path / "repo"
        (repo / ".luxe").mkdir(parents=True)
        mf = repo / ".luxe" / "memory.md"
        mf.write_text("hand-written notes\n")
        ephemeral.enable()
        project_mem.splice_block(repo, "notes", "generated")
        assert mf.read_text() == "hand-written notes\n"

    def test_notes_are_skipped_even_on_demand(self):
        """`/note` bypasses the config toggle and the turn floor because
        asking IS consent — but it must not override a session the user
        started as write-nothing."""
        from luxe.chat.notes import skip_reason

        class _S:
            repo_path = "/tmp/repo"
            project_kind = "git"
            turns: list = []

        class _C:
            notes = True

        ephemeral.enable()
        assert skip_reason(_S(), _C(), on_demand=True) == "ephemeral session"

    def test_the_startup_notice_states_both_halves(self):
        """It has to say what is suppressed AND what is not — a user who
        thinks this disables the write tools will hand it a repo-editing task."""
        assert ephemeral.startup_notice() == ""
        ephemeral.enable()
        notice = ephemeral.startup_notice()
        assert "no transcript" in notice and "debug.log" in notice
        assert "Cannot be resumed" in notice
        assert "/write" in notice          # write tools unaffected
        assert "repo lock" in notice       # the one surviving artifact


class TestTheFlagItself:
    def test_it_is_off_by_default(self):
        assert ephemeral.is_ephemeral() is False

    def test_it_toggles_both_ways(self):
        """`/ephemeral` is a mid-session toggle (2026-08-11, by request). The
        earlier design made it one-way on the argument that a session cannot
        un-write what it already wrote — `purge_session` answers that instead,
        and turning it back off simply resumes persistence."""
        ephemeral.enable()
        assert ephemeral.is_ephemeral() is True
        ephemeral.disable()
        assert ephemeral.is_ephemeral() is False
        ephemeral.set_enabled(True)
        assert ephemeral.is_ephemeral() is True

    def test_turning_it_off_resumes_writing(self, home, tmp_path):
        ephemeral.enable()
        meta = session_store.new_session(repo_path=str(tmp_path))
        session_store.append_turn(meta.session_id, "user", text="unrecorded")
        assert not session_store.session_dir(meta.session_id).exists()

        ephemeral.disable()
        session_store.append_turn(meta.session_id, "user", text="recorded")
        tp = session_store.session_dir(meta.session_id) / "transcript.jsonl"
        assert tp.is_file()
        body = tp.read_text()
        # The hole is real and permanent: what happened while it was on is gone.
        assert "recorded" in body
        assert "unrecorded" not in body


class TestPurge:
    """`/ephemeral` mid-session removes what this session already wrote."""

    def test_it_removes_this_sessions_directories(self, home, tmp_path):
        from luxe.run_state import runs_root

        meta = session_store.new_session(repo_path=str(tmp_path))
        sid = meta.session_id
        session_store.append_turn(sid, "user", text="said before the toggle")
        append_event(f"{sid}-0", "tool_call", name="read_file")
        append_event(f"{sid}-1", "tool_call", name="grep")
        assert session_store.session_dir(sid).exists()

        removed = ephemeral.purge_session(sid, str(tmp_path))

        assert not session_store.session_dir(sid).exists()
        assert not (runs_root() / f"{sid}-0").exists()
        assert not (runs_root() / f"{sid}-1").exists()
        assert len(removed) == 3

    def test_it_leaves_other_sessions_alone(self, home, tmp_path):
        """Prefix-matching run ids must not reach a neighbour's data."""
        from luxe.run_state import runs_root

        mine = session_store.new_session(repo_path=str(tmp_path))
        theirs = session_store.new_session(repo_path=str(tmp_path))
        append_event(f"{theirs.session_id}-0", "tool_call", name="read_file")

        ephemeral.purge_session(mine.session_id, str(tmp_path))

        assert session_store.session_dir(theirs.session_id).exists()
        assert (runs_root() / f"{theirs.session_id}-0").exists()

    def test_it_never_touches_project_memory(self, home, tmp_path):
        """The hard line. `.luxe/memory.md` interleaves luxe's machine blocks
        with the user's own curated notes, so a mode that writes nothing must
        not become a mode that deletes hand-written text."""
        repo = tmp_path / "repo"
        (repo / ".luxe").mkdir(parents=True)
        mf = repo / ".luxe" / "memory.md"
        mf.write_text("curated by hand\n<!-- luxe:notes -->\nauto\n<!-- /luxe:notes -->\n")
        meta = session_store.new_session(repo_path=str(repo))

        ephemeral.purge_session(meta.session_id, str(repo))

        assert mf.is_file()
        assert "curated by hand" in mf.read_text()

    def test_it_is_safe_on_a_session_that_wrote_nothing(self, home, tmp_path):
        ephemeral.enable()
        meta = session_store.new_session(repo_path=str(tmp_path))
        assert ephemeral.purge_session(meta.session_id, str(tmp_path)) == []

    def test_an_empty_session_id_is_a_no_op(self, home):
        assert ephemeral.purge_session("", "") == []

    def test_the_benchmark_path_is_unaffected(self, home, tmp_path):
        """`enable()` is called only from the chat launcher. Without it every
        writer behaves as it always has — this is the guard against the flag
        leaking into a bench run and silently discarding its telemetry."""
        append_event("bench-run", "tool_call", name="read_file")
        p = run_dir("bench-run") / "events.jsonl"
        assert p.is_file()
        assert json.loads(p.read_text().splitlines()[0])["name"] == "read_file"
