"""Chat front-end robustness (2026-09 review, PR "chat front-end robustness").

One regression test (or a small group) per finding, numbered as in the PR
body. Every test here FAILED against the pre-fix tree. No model, no network:
`run_single` / the Backend are stubbed, HOME is a tmp dir.
"""

from __future__ import annotations

import asyncio
import io
import time
from pathlib import Path

import pytest
from rich.console import Console

from luxe import ephemeral
from luxe.backend import BackendError
from luxe.chat import commands as cmd
from luxe.chat import repl as repl_mod
from luxe.chat import slots as slots_mod
from luxe.chat.render import ChatCancelled
from luxe.chat.session import ChatSession, ChatTurn
from luxe.config import PipelineConfig, RoleConfig
from luxe.memory import session as session_store
from luxe.state import ledger as ledger_mod

NASTY = "oops [/x] and [/] and [bold]"


class FakeBackend:
    def __init__(self, base_url="", model="", timeout_s=600.0, api_key="",
                 **kw):
        self.base_url = base_url
        self.model = model
        self.timeout_s = timeout_s
        self.api_key = api_key
        self.body_extras = {}

    def health(self, timeout_s=None):
        return True

    def list_models(self):
        return ["Champ"]

    list_full_calls = 0
    list_full_raises = False

    def list_models_full(self):
        type(self).list_full_calls += 1
        if type(self).list_full_raises:
            raise OSError("connect timeout")
        return [{"id": "Champ"}]

    def unload_all_loaded(self, *, except_for=None):
        return {}

    def loaded_models(self):
        return []

    def unload_model(self, model_id):
        return True

    def thermal_guard(self, *a, **k):
        return True


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    FakeBackend.list_full_calls = 0
    FakeBackend.list_full_raises = False
    monkeypatch.setattr(slots_mod, "Backend", FakeBackend)
    # Never let a repair attempt or a real GC thread reach this machine.
    monkeypatch.setattr(slots_mod.SlotManager, "try_self_repair",
                        lambda self, reason="", **k: None)
    ephemeral._reset_for_tests()
    yield home
    ephemeral._reset_for_tests()


def _cfg() -> PipelineConfig:
    return PipelineConfig(models={"monolith": "Champ"},
                          roles={"monolith": RoleConfig(model_key="monolith")})


def _console():
    out = io.StringIO()
    return Console(file=out, force_terminal=False, width=200), out


def _reader(lines):
    it = iter(lines)

    def read():
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return read


def _repl(lines, *, repo="", on_project=None, cfg=None, **kw):
    console, out = _console()
    repl_mod.run_chat_repl(cfg or _cfg(), repo, frozenset(), console=console,
                           keep_loaded=True, reader=_reader(lines),
                           infer_task_type=lambda m: "review",
                           on_project=on_project, **kw)
    return out.getvalue()


def _ctx(session=None, **kw):
    console, out = _console()
    s = session or ChatSession()
    c = cmd.CommandContext(console=console, session=s,
                           slots=slots_mod.SlotManager(_cfg()), **kw)
    c._out = out  # type: ignore[attr-defined]
    return c


class _Result:
    """Enough of an AgentResult for the front-ends."""
    final_text = "fine"
    step_texts: list = []
    steps = 1
    tool_calls_total = 0
    tool_calls: list = []
    wall_s = 0.1
    completion_tokens = 1
    prompt_tokens = 1
    last_prompt_tokens = 0
    peak_context_pressure = 0.0
    final_context_pressure = 0.0
    aborted = False
    abort_reason = ""
    cost_usd = 0.0


# --- 1. Rich markup in user/model/exception text ---------------------------

def test_1_repl_backend_error_with_markup_does_not_end_the_session(monkeypatch):
    def _raise(*a, **k):
        raise BackendError(f"oMLX returned 500: {NASTY}")

    monkeypatch.setattr(repl_mod, "run_single", _raise)
    out = _repl(["hello", "again"])
    # Both prompts ran: the handler's print used to raise MarkupError from
    # INSIDE the except, escaping the loop on the first failure.
    assert out.count("oMLX returned 500") == 2
    assert "[/x]" in out


def test_1_resume_replays_a_transcript_containing_markup():
    from luxe.chat.resume import resume_into

    meta = session_store.new_session()
    session_store.append_turn(meta.session_id, "user", text=f"q {NASTY}")
    session_store.append_turn(meta.session_id, "assistant", text=f"a {NASTY}")
    console, out = _console()
    live = ChatSession()
    assert resume_into(meta.session_id, live, console)
    assert "[/x]" in out.getvalue()


def test_1_ledger_render_rich_escapes_model_text():
    led = ledger_mod.Ledger(goal=NASTY, decided=[NASTY], files=["a[/x].py"])
    console, out = _console()
    console.print(ledger_mod.render_rich(led))
    assert "[/x]" in out.getvalue()


def test_1_tool_line_escapes_error_and_name():
    from luxe.chat.render import format_tool_call
    from luxe.tools.base import ToolCall

    tc = ToolCall(id="1", name="bad[/x]", arguments={}, error=NASTY)
    console, out = _console()
    console.print(format_tool_call(tc))
    assert "[/x]" in out.getvalue()


@pytest.mark.parametrize("line", [
    f"/sys add {NASTY}", f"/goal {NASTY}", f"/plan {NASTY}",
    "/attach /no/such[/x]", "/memory [/x]", "/theme [/x]",
])
def test_1_commands_echoing_user_text_escape_it(line):
    s = ChatSession(write_enabled=True, repo_path="/tmp")
    c = _ctx(s)
    cmd.dispatch(line, c)                       # raised MarkupError before
    if line.startswith("/sys"):
        cmd.dispatch("/sys list", c)
    assert "[/x]" in c._out.getvalue()


def test_1_retry_preview_escapes_user_text():
    s = ChatSession(turns=[ChatTurn(user=f"hi {NASTY}")])
    c = _ctx(s)
    res = cmd.dispatch("/retry", c)
    assert res.submit and "[/x]" in c._out.getvalue()


# --- 2. commands / plan / goal contained like turns ------------------------

def test_2_a_crashing_command_does_not_end_the_line_repl(monkeypatch):
    real = cmd.dispatch

    def _dispatch(line, ctx):
        if line.startswith("/doctor"):
            raise OSError(60, "Operation timed out")
        return real(line, ctx)

    monkeypatch.setattr(cmd, "dispatch", _dispatch)
    out = _repl(["/doctor", "/help"])
    assert "command failed" in out and "Operation timed out" in out
    assert "luxe chat commands" in out        # the session kept going


def test_2_ctrl_c_during_a_command_does_not_end_the_line_repl(monkeypatch):
    real = cmd.dispatch

    def _dispatch(line, ctx):
        if line.startswith("/pull"):
            raise KeyboardInterrupt
        return real(line, ctx)

    monkeypatch.setattr(cmd, "dispatch", _dispatch)
    out = _repl(["/pull x", "/help"])
    assert "interrupted" in out
    assert "luxe chat commands" in out


# --- 3/4. TUI quit: notes before unload; quitting mid-turn cancels ---------

def _app(tmp_path, keep_loaded=False):
    pytest.importorskip("textual")
    from luxe.chat.tui import ChatApp

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    session = ChatSession(repo_path=str(repo))
    meta = session_store.new_session(repo_path=str(repo))
    session.session_id = meta.session_id
    return ChatApp(_cfg(), str(repo), frozenset(), session=session,
                   slots=slots_mod.SlotManager(_cfg()), infer=lambda m: "review",
                   keep_loaded=keep_loaded)


def test_3_quit_does_not_unload_before_the_session_notes(tmp_path):
    async def scenario():
        app = _app(tmp_path, keep_loaded=False)
        calls = []
        app.slots.unload_all = lambda: calls.append("unload")
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_quit_app()
            await pilot.pause()
        assert calls == []          # run_chat_app's finally owns the unload
    asyncio.run(scenario())


def test_3_session_notes_distil_is_bounded(monkeypatch):
    from luxe.chat import notes as notes_mod

    monkeypatch.setattr(notes_mod, "skip_reason", lambda *a, **k: "")
    monkeypatch.setattr(notes_mod, "distil",
                        lambda session, backend: time.sleep(3) or "- x")

    class _Slots:
        def backend_for(self, slot):
            return object()

    console, _ = _console()
    t0 = time.monotonic()
    res = notes_mod.run_session_notes(ChatSession(repo_path="/r"), _Slots(),
                                      _cfg(), console, timeout_s=0.2)
    assert time.monotonic() - t0 < 2
    assert res.written is None and "timed out" in res.skipped


def test_4_quitting_mid_turn_cancels_the_turn(tmp_path):
    async def scenario():
        app = _app(tmp_path, keep_loaded=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._busy = True
            app.action_quit_app()
            assert app.cancel.requested is True
            assert app.quit_while_busy is True
            app._busy = False
    asyncio.run(scenario())


# --- 5. history fold + current_request echo are capped ----------------------

def test_5_a_huge_recent_turn_is_capped_head_and_tail():
    from luxe.chat.summarize import fold_history

    big = "HEAD" + "x" * 200_000 + "TAIL"
    out = fold_history([(big, "ok")])
    assert len(out) < 10_000
    assert "HEAD" in out and "TAIL" in out and "elided" in out


def test_5_current_request_echo_is_capped():
    s = ChatSession(write_enabled=False)
    msg = "ASK" + "y" * 200_000 + "END"
    extra, _ = s.build_extra_context(msg)
    echo = extra.split("<current_request>")[1]
    assert len(echo) < 5_000
    assert "ASK" in echo and "END" in echo


# --- 6. spend counts requests billed by a turn that did not complete -------

def test_6_backend_keeps_a_running_spend():
    from luxe.backend import Backend

    b = Backend(base_url="http://127.0.0.1:9")
    assert b.cost_total_usd == 0.0
    b._note_cost(0.25)
    b._note_cost(None)
    assert b.cost_total_usd == pytest.approx(0.25)


def test_6_a_turn_that_errors_after_billing_still_counts(monkeypatch):
    seen = {}

    def _bill_then_fail(backend, *a, **k):
        backend.cost_total_usd = getattr(backend, "cost_total_usd", 0.0) + 0.5
        raise BackendError("upstream 502 after a billed step")

    monkeypatch.setattr(repl_mod, "run_single", _bill_then_fail)
    real_new = ChatSession.__init__

    def _capture(self, *a, **k):
        real_new(self, *a, **k)
        seen["session"] = self

    monkeypatch.setattr(ChatSession, "__init__", _capture)
    _repl(["hello"])
    assert seen["session"].session_cost_usd == pytest.approx(0.5)


# --- 7. /project makes the session the live source ---------------------------

def test_7_project_switch_moves_languages_for_the_next_turn(monkeypatch, tmp_path):
    got = {}

    def _run_single(backend, role, **k):
        got["languages"] = k.get("languages")
        return _Result()

    monkeypatch.setattr(repl_mod, "run_single", _run_single)
    target = tmp_path / "other"
    target.mkdir()

    def _attach(t):
        return {"root": str(target), "kind": "dir", "label": "project",
                "files": 1, "symbols": 0, "truncated": "", "used_git": False,
                "languages": frozenset({"python"})}

    _repl([f"/project {target}", "hello"], on_project=_attach)
    assert got["languages"] == frozenset({"python"})


# --- 8. /goal: one failure path; a refused turn stops the loop -------------

def _goal_session():
    s = ChatSession(write_enabled=True, goal="do it", goal_active=True,
                    goal_max_rounds=10)
    s.session_id = session_store.new_session().session_id
    return s


def test_8_a_refused_turn_stops_the_goal_at_once():
    s = _goal_session()
    calls = []

    def _turn(*a, **k):
        calls.append(1)
        return repl_mod.TurnOutcome(crashed=True, final_text="spend cap")

    console, _ = _console()
    repl_mod._run_goal_loop(s, slots_mod.SlotManager(_cfg()), _cfg(),
                            frozenset(), console, None, lambda m: "review",
                            None, run_turn=_turn)
    assert calls == [1]
    assert s.goal_active is False


def test_8_a_backend_error_round_runs_the_recovery_and_records_it(monkeypatch):
    s = _goal_session()
    sm = slots_mod.SlotManager(_cfg())
    recovered = []
    monkeypatch.setattr(sm, "note_turn_failure",
                        lambda: recovered.append(1) or None)

    def _turn(*a, **k):
        raise BackendError("oMLX returned 500: No module named 'x'")

    console, out = _console()
    repl_mod._run_goal_loop(s, sm, _cfg(), frozenset(), console, None,
                            lambda m: "review", None, run_turn=_turn)
    assert recovered, "degrade/repair never ran for a failed goal round"
    loaded = session_store.load_session(s.session_id)
    assert any(r["kind"] == "error" for r in loaded[1])
    assert s.goal_active is False            # paused after the crash budget


# --- 9. a failed catalog GET is remembered ---------------------------------

def test_9_catalog_failure_is_cached_for_a_while():
    FakeBackend.list_full_raises = True
    sm = slots_mod.SlotManager(_cfg())
    assert sm.catalog() == []
    assert sm.catalog() == []
    assert FakeBackend.list_full_calls == 1


# --- 10/11. /ephemeral off restores the session; ledger lives in memory ----

def test_10_ephemeral_off_restores_meta_and_the_debug_log(tmp_path):
    from luxe.chat import debuglog

    repo = tmp_path / "r"
    repo.mkdir()
    s = ChatSession(repo_path=str(repo))
    meta = session_store.new_session(repo_path=str(repo))
    s.session_id = meta.session_id
    log = debuglog.install(session_store.session_dir(s.session_id))
    try:
        c = _ctx(s, session_log=log)
        cmd.dispatch("/ephemeral on", c)
        assert session_store.load_meta(s.session_id) is None
        cmd.dispatch("/ephemeral off", c)
        assert session_store.load_meta(s.session_id) is not None
        assert log.path is not None and log._handler is not None
        session_store.append_turn(s.session_id, "user", text="after")
        assert session_store.load_session(s.session_id) is not None
    finally:
        debuglog.uninstall(log)


def test_11_ephemeral_ledger_is_kept_in_memory():
    ephemeral.enable()
    ledger_mod.apply_update("sid1", {"completed": ["built it"]})
    ledger_mod.record_files("sid1", ["a.py"])
    led = ledger_mod.load("sid1")
    assert led.completed == ["built it"] and led.files == ["a.py"]
    assert not (session_store.session_dir("sid1") / "ledger.json").exists()


# --- 12. unguarded ephemeral writes -----------------------------------------

def test_12_theme_is_not_persisted_when_ephemeral(monkeypatch, tmp_path):
    from luxe.chat import theme as theme_mod

    pref = tmp_path / "theme-pref"
    monkeypatch.setattr(theme_mod, "_PREF_PATH", pref)
    before = theme_mod.active_palette()
    ephemeral.enable()
    try:
        c = _ctx()
        cmd.dispatch("/theme mono", c)
        assert not pref.exists()
        assert "not saved" in c._out.getvalue()
    finally:
        theme_mod.set_palette(before)


def test_12_memory_add_under_ephemeral_says_it_saved_nothing(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    ephemeral.enable()
    c = _ctx(ChatSession(repo_path=str(repo)))
    cmd.dispatch("/memory add remember this", c)
    out = c._out.getvalue()
    assert "not saved" in out and "✓ saved" not in out


def test_12_memory_edit_under_ephemeral_does_not_create_the_file(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    ephemeral.enable()
    ran = []
    c = _ctx(ChatSession(repo_path=str(repo)),
             run_external=lambda argv: ran.append(argv) or 0)
    cmd.dispatch("/memory edit", c)
    assert not (repo / ".luxe" / "memory.md").exists()
    assert ran == []


# --- 13. the cancel token is reset per command -------------------------------

def test_13_an_interrupted_turn_does_not_cancel_the_next_command(monkeypatch, tmp_path):
    seen = {}

    def _hook(console, cfg, session, cancel=None):
        seen["cancel"] = cancel

        def _git(kind, deep=None):
            seen["requested_at_call"] = cancel.requested
        return _git

    monkeypatch.setattr(repl_mod, "_make_git_analysis_hook", _hook)

    def _interrupted(*a, **k):
        seen["cancel"].requested = True
        raise ChatCancelled()

    monkeypatch.setattr(repl_mod, "run_single", _interrupted)
    repo = tmp_path / "r"
    repo.mkdir()
    _repl(["hello", "/gitaudit"], repo=str(repo))
    assert seen["requested_at_call"] is False


# --- 14. /clear resets the carried state and marks the transcript ----------

def test_14_clear_resets_ledger_plan_and_is_respected_by_resume():
    from luxe.chat.resume import _pair_turns

    s = ChatSession(plan_text="old plan")
    s.session_id = session_store.new_session().session_id
    ledger_mod.apply_update(s.session_id, {"completed": ["old work"]})
    s.turns.append(ChatTurn(user="before", assistant="x"))
    session_store.append_turn(s.session_id, "user", text="before")
    session_store.append_turn(s.session_id, "assistant", text="x")
    cmd.dispatch("/clear", _ctx(s))
    assert s.plan_text == ""
    assert ledger_mod.load(s.session_id).is_empty()
    session_store.append_turn(s.session_id, "user", text="after")
    session_store.append_turn(s.session_id, "assistant", text="y")
    turns = _pair_turns(session_store.load_session(s.session_id)[1])
    assert [t.user for t in turns] == ["after"]


# --- 15. /resume persists and does not duplicate ---------------------------

def test_15_resume_persists_and_refuses_a_duplicate():
    from luxe.chat.resume import resume_into

    old = session_store.new_session().session_id
    session_store.append_turn(old, "user", text="q1")
    session_store.append_turn(old, "assistant", text="a1")
    live = ChatSession()
    live.session_id = session_store.new_session().session_id
    console, _ = _console()
    assert resume_into(old, live, console)
    resume_into(old, live, console)
    assert [t.user for t in live.turns] == ["q1"]
    recs = session_store.load_session(live.session_id)[1]
    assert [r["text"] for r in recs if r["kind"] == "user"] == ["q1"]


# --- 16. /memory edit: $EDITOR is a command line, run via the front-end ----

def test_16_memory_edit_splits_editor_and_uses_the_runner(monkeypatch, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    monkeypatch.setenv("EDITOR", "code --wait")
    ran = []
    c = _ctx(ChatSession(repo_path=str(repo)),
             run_external=lambda argv: ran.append(argv) or 0)
    cmd.dispatch("/memory edit", c)
    assert ran and ran[0][:2] == ["code", "--wait"]
    assert ran[0][2].endswith("memory.md")


# --- 17. no quadratic stream buffer; git status off the render thread ------

def test_17_git_status_refreshes_in_the_background(monkeypatch):
    from luxe.chat import status as status_mod

    def _slow(repo, *args):
        time.sleep(1.0)
        return None

    monkeypatch.setattr(status_mod, "_run_git", _slow)
    status_mod._git_cache.clear()
    t0 = time.monotonic()
    status_mod.git_info("/some/repo-bg", background=True)
    assert time.monotonic() - t0 < 0.5


def test_17_tui_stream_buffer_is_chunked_and_bounded(tmp_path, monkeypatch):
    long = ["tok " * 50] * 200

    def _streaming(backend, role, **k):
        for chunk in long:
            k["on_token"](chunk)
        return _Result()

    monkeypatch.setattr(repl_mod, "run_single", _streaming)

    async def scenario():
        app = _app(tmp_path, keep_loaded=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._run_turn("hello")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(app._stream_tail) <= 400
            assert "".join(app._stream_parts) == "".join(long)
    asyncio.run(scenario())


# --- 18. /plan never drops plan.md into $HOME --------------------------------

def test_18_plan_save_without_a_project_goes_under_luxe(monkeypatch, tmp_path, _env):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    s = ChatSession(repo_path="", project_kind="none", session_id="abcdef123")
    path = repl_mod._write_plan_file(s, "# plan")
    assert not (cwd / "plan.md").exists()
    assert path is not None and str(path).startswith(str(_env / ".luxe"))
    ephemeral.enable()
    assert repl_mod._write_plan_file(s, "# plan") is None


# --- 19. /attach survives a failed turn -------------------------------------

def test_19_attachments_are_restaged_when_the_turn_fails(monkeypatch, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("attached body")

    def _raise(*a, **k):
        raise BackendError("oMLX call failed: ConnectError")

    monkeypatch.setattr(repl_mod, "run_single", _raise)
    out = _repl([f"/attach {f}", "summarise it", "/attach"])
    assert "still staged" in out
    assert "pending attachments" in out        # `/retry` would resend it


# --- 20. help text matches behaviour -----------------------------------------

def test_20_sys_help_names_remove():
    row = next(r for r in cmd._HELP_ROWS if r[0] == "/sys")
    assert "remove" in row[1]


def test_20_goal_stop_typed_mid_goal_in_the_tui_stops_it(tmp_path):
    from textual.widgets import Input

    async def scenario():
        app = _app(tmp_path, keep_loaded=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._busy = True
            app.session.goal_active = True
            app.on_input_submitted(Input.Submitted(app._input, "/goal stop"))
            assert app.session.goal_active is False
            assert app._queue == []
            app._busy = False
    asyncio.run(scenario())


# --- 21. session GC is wired, in the background, and ephemeral-aware -------

def test_21_session_gc_runs_at_start_but_not_when_ephemeral(monkeypatch):
    ran = []
    monkeypatch.setattr(session_store, "gc_sessions",
                        lambda **k: ran.append(1) or 0)
    repl_mod.start_session_gc()
    deadline = time.monotonic() + 2
    while not ran and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ran == [1]
    ephemeral.enable()
    repl_mod.start_session_gc()
    time.sleep(0.1)
    assert ran == [1]


def test_21_repl_start_invokes_session_gc(monkeypatch):
    called = []
    monkeypatch.setattr(repl_mod, "start_session_gc", lambda: called.append(1))
    _repl([])
    assert called == [1]


def test_markup_safety_net_in_the_tui_write(tmp_path):
    async def scenario():
        app = _app(tmp_path, keep_loaded=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.write(f"[red]{NASTY}[/]")      # an unescaped miss somewhere
            await pilot.pause()
            assert app.is_running
    asyncio.run(scenario())


def test_1_tui_queued_message_with_markup_does_not_kill_the_app(tmp_path):
    from textual.widgets import Input

    async def scenario():
        app = _app(tmp_path, keep_loaded=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._busy = True
            app.on_input_submitted(Input.Submitted(app._input, f"later {NASTY}"))
            await pilot.pause()
            assert app.is_running
            assert app._queue
            app._busy = False
            app._queue.clear()
    asyncio.run(scenario())

