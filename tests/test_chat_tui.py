"""Textual TUI smoke/behaviour tests (driven via asyncio.run + Pilot, so they
don't depend on pytest-asyncio mode config). run_single is stubbed — no model."""

from __future__ import annotations

import asyncio

import pytest

textual = pytest.importorskip("textual")

from textual.widgets import Input, RichLog  # noqa: E402

from luxe.chat import repl as _repl  # noqa: E402
from luxe.chat import slots as slots_mod  # noqa: E402
from luxe.chat.session import ChatSession  # noqa: E402
from luxe.chat.tui import ChatApp, StatusBar  # noqa: E402
from luxe.config import PipelineConfig, RoleConfig  # noqa: E402
from luxe.memory import session as session_store  # noqa: E402


class _FakeBackend:
    def __init__(self, base_url="", model="", timeout_s=600.0, api_key=""):
        self.base_url = base_url
        self.model = model
        self.timeout_s = timeout_s
        self.api_key = api_key

    def unload_all_loaded(self, *, except_for=None):
        return {}

    def thermal_guard(self, *a, **k):
        return True


class _FakeResult:
    final_text = "**Hello** from the model"
    steps = 1
    tool_calls_total = 0
    wall_s = 0.5
    completion_tokens = 10
    prompt_tokens = 20
    peak_context_pressure = 0.1
    final_context_pressure = 0.1
    last_prompt_tokens = 5000  # server-reported usage (RunResult field)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(slots_mod, "Backend", _FakeBackend)


def _make_app(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = PipelineConfig(models={"monolith": "Champ"},
                         roles={"monolith": RoleConfig(model_key="monolith")})
    session = ChatSession(repo_path=str(repo))
    meta = session_store.new_session(repo_path=str(repo), project_hash="h", slot_models={})
    session.session_id = meta.session_id
    return ChatApp(cfg, str(repo), frozenset(), session=session,
                   slots=slots_mod.SlotManager(cfg), infer=lambda m: "review",
                   keep_loaded=True)


def test_boots(tmp_path):
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#transcript", RichLog) is not None
            assert app.query_one("#prompt", Input) is not None
            assert app.query_one("#status", StatusBar) is not None
    asyncio.run(scenario())


def test_turn_renders_final(tmp_path, monkeypatch):
    monkeypatch.setattr(_repl, "run_single", lambda *a, **k: _FakeResult())

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt", Input).value = "hi there"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            # the turn ran through the core and persisted to history
            assert app.session.turns
            assert app.session.turns[-1].assistant == "**Hello** from the model"
            assert not app._busy
    asyncio.run(scenario())


def test_command_dispatch(tmp_path):
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt", Input).value = "/compact"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.session.compact is True
    asyncio.run(scenario())


def test_typeahead_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(_repl, "run_single", lambda *a, **k: _FakeResult())

    async def scenario():
        from textual.widgets import Input
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._busy = True  # simulate a running task
            app.on_input_submitted(Input.Submitted(app._input, "later msg"))
            assert ("later msg", "later msg") in app._queue  # queued, not run
            app._busy = False
            app._maybe_drain()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._queue == []                    # drained
            assert any(t.user == "later msg" for t in app.session.turns)
    asyncio.run(scenario())


def test_action_cancel_sets_token_when_busy(tmp_path):
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._busy = True
            app.action_cancel()
            assert app.cancel.requested is True
    asyncio.run(scenario())


# --- ctrl+c (2026-08-13): clear the input, Claude-CLI style ------------------


def test_ctrl_c_clears_the_input_when_idle(tmp_path):
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._input.focus()
            app._input.value = "half-typed question"
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app._input.value == ""
            assert app.is_running                  # it must NOT quit
            assert app.cancel.requested is False   # nothing was cancelled
    asyncio.run(scenario())


def test_ctrl_c_drops_a_pending_paste_chip_and_its_text(tmp_path):
    """The chip lives in the input but its TEXT lives on the app; clearing one
    without the other would resurrect the paste at the next submit."""
    from textual import events

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._input.focus()
            app._input._on_paste(events.Paste("a\nb\nc"))
            await pilot.pause()
            assert app._paste_chunks                # buffered
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app._input.value == ""
            assert app._paste_chunks == []
    asyncio.run(scenario())


def test_ctrl_c_cancels_the_turn_while_busy(tmp_path):
    """Cancel keeps precedence where the key already had a meaning."""
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._input.value = "typed while running"
            app._busy = True
            app.action_interrupt()
            assert app.cancel.requested is True
            assert app._input.value == "typed while running"   # not cleared
    asyncio.run(scenario())


def test_ctrl_c_on_empty_input_is_a_noop(tmp_path):
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.is_running                  # never quits
            assert app.cancel.requested is False
    asyncio.run(scenario())


def test_ctrl_c_resets_history_navigation(tmp_path):
    """A cleared line is not a draft: arrowing down must not repopulate it."""
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            inp = app._input
            inp.focus()
            inp.history_remember("earlier")
            inp.value = "draft"
            inp._history_prev()                    # now recalling "earlier"
            assert inp.value == "earlier"
            app.action_interrupt()
            assert inp.value == ""
            inp._history_next()                    # not navigating any more
            assert inp.value == ""
    asyncio.run(scenario())


def test_tick_folds_live_ctx_pressure(tmp_path):
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._gen_started = 1.0
            app._ctx_pressure = 0.42
            app._tick()
            assert abs(app.status.ctx_pressure - 0.42) < 1e-9
            assert app.status.has_turn is True
    asyncio.run(scenario())


def test_null_status_updates_activity_not_transcript(tmp_path):
    from luxe.chat.tui import LogConsole

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            st = LogConsole(app).status("gathering…")
            with st:
                st.update("analyzing… read_file (3)")
                assert "read_file (3)" in (app._activity or "")
            assert app._activity is None  # cleared on exit
    asyncio.run(scenario())


def test_scroll_bindings_no_error(tmp_path):
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_scroll_up()
            app.action_scroll_down()
            app.action_scroll_end()  # must not raise
    asyncio.run(scenario())


def test_tick_survives_open_modal(tmp_path):
    """Regression: the timer/refresh must not crash when a PromptScreen modal is
    on top (App.query_one would otherwise miss the base-screen #generating)."""
    from luxe.chat.tui import PromptScreen

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._begin_busy()
            app.push_screen(PromptScreen("clone url?"))
            await pilot.pause()
            app._tick()            # must not raise (modal active)
            app.refresh_status()   # must not raise
            app.screen.dismiss("")
            await pilot.pause()
            app._end_busy()
    asyncio.run(scenario())


def test_paste_single_line_inserts_into_input(tmp_path):
    from textual import events

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._input.focus()
            app._input._on_paste(events.Paste("one line"))
            await pilot.pause()
            assert app._input.value == "one line"
            assert app._paste_chunks == []          # nothing buffered
    asyncio.run(scenario())


def test_paste_multiline_buffers_as_chip(tmp_path):
    from textual import events

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._input.focus()
            app._input._on_paste(events.Paste("line a\nline b\nline c"))
            await pilot.pause()
            assert "[pasted 3 lines]" in app._input.value
            assert app._paste_chunks == [("[pasted 3 lines]",
                                          "line a\nline b\nline c")]
    asyncio.run(scenario())


def test_paste_chip_expands_at_submit(tmp_path, monkeypatch):
    captured = {}

    def fake_run_single(backend, role_cfg, **kw):
        captured["goal"] = kw.get("goal")
        return _FakeResult()

    monkeypatch.setattr(_repl, "run_single", fake_run_single)

    from textual import events

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._input.focus()
            app._input.value = "explain this: "
            app._input.cursor_position = len(app._input.value)
            app._input._on_paste(events.Paste("x = 1\ny = 2"))
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert captured["goal"] == "explain this: x = 1\ny = 2"
            assert app._paste_chunks == []          # consumed at submit
            assert app.session.turns[-1].user == "explain this: x = 1\ny = 2"
    asyncio.run(scenario())


def test_paste_deleted_chip_still_appends_text(tmp_path, monkeypatch):
    monkeypatch.setattr(_repl, "run_single", lambda *a, **k: _FakeResult())

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._paste_chunks = [("[pasted 2 lines]", "a\nb")]
            app._input.value = "look:"               # user erased the chip
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.session.turns[-1].user == "look:\n\na\nb"
    asyncio.run(scenario())


def test_resume_on_mount_replays_prior_session(tmp_path):
    prior = session_store.new_session(repo_path="/tmp/x")
    session_store.append_turn(prior.session_id, "user", text="old q", slot="chat")
    session_store.append_turn(prior.session_id, "assistant", text="old a",
                              run_id="r0")

    async def scenario():
        repo = tmp_path / "repo2"
        repo.mkdir()
        cfg = PipelineConfig(models={"monolith": "Champ"},
                             roles={"monolith": RoleConfig(model_key="monolith")})
        session = ChatSession(repo_path=str(repo))
        meta = session_store.new_session(repo_path=str(repo))
        session.session_id = meta.session_id
        app = ChatApp(cfg, str(repo), frozenset(), session=session,
                      slots=slots_mod.SlotManager(cfg),
                      infer=lambda m: "review", keep_loaded=True,
                      resume_session_id=prior.session_id)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(app.session.turns) == 1
            assert app.session.turns[0].user == "old q"
            assert app.session.turns[0].assistant == "old a"
    asyncio.run(scenario())


def test_resume_command_inside_tui(tmp_path):
    prior = session_store.new_session(repo_path="/tmp/x")
    session_store.append_turn(prior.session_id, "user", text="cmd q", slot="chat")
    session_store.append_turn(prior.session_id, "assistant", text="cmd a",
                              run_id="r0")

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._input.value = f"/resume {prior.session_id}"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert any(t.user == "cmd q" and t.assistant == "cmd a"
                       for t in app.session.turns)
    asyncio.run(scenario())


def test_prompt_user_requires_worker_thread(tmp_path):
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # calling from the UI/main thread must raise (deadlock guard)
            with pytest.raises(AssertionError):
                app.prompt_user("pick?")
    asyncio.run(scenario())


def test_turn_crash_does_not_kill_the_app(tmp_path, monkeypatch):
    """Regression (2026-07-29): an uncaught OSError from the turn path — a repo
    walk hitting an unreachable network-backed dir — unwound into Textual's
    worker and killed the whole session. It must now report and survive."""
    def _boom(*a, **k):
        raise OSError(60, "Operation timed out")

    monkeypatch.setattr(_repl, "run_single", _boom)

    async def scenario():
        app = _make_app(tmp_path)
        written: list = []
        async with app.run_test() as pilot:
            await pilot.pause()
            real_write = app.write
            app.write = lambda r: (written.append(r), real_write(r))[1]
            app.query_one("#prompt", Input).value = "hi there"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.is_running                       # session survived
            assert not app._busy                        # busy state cleaned up
            assert any("turn failed" in str(w) for w in written)
            assert any("Operation timed out" in str(w) for w in written)
    asyncio.run(scenario())


def test_command_crash_does_not_kill_the_app(tmp_path, monkeypatch):
    """Same contract for the command worker (/plan, /goal, gitkit …)."""
    from luxe.chat import commands as cmd_mod

    def _boom(*a, **k):
        raise OSError(60, "Operation timed out")

    monkeypatch.setattr(cmd_mod, "dispatch", _boom)

    async def scenario():
        app = _make_app(tmp_path)
        written: list = []
        async with app.run_test() as pilot:
            await pilot.pause()
            real_write = app.write
            app.write = lambda r: (written.append(r), real_write(r))[1]
            app.query_one("#prompt", Input).value = "/compact"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.is_running
            assert not app._busy
            assert any("turn failed" in str(w) for w in written)
    asyncio.run(scenario())


def test_retry_runs_a_turn_from_the_command_worker(tmp_path, monkeypatch):
    """`/retry` hands a message back through CommandResult.submit; the TUI must
    run it as a real turn, not print it."""
    monkeypatch.setattr(_repl, "run_single", lambda *a, **k: _FakeResult())

    async def scenario():
        from luxe.chat.session import ChatTurn

        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.session.add_turn(ChatTurn(user="do the thing", assistant="ok"))
            app.query_one("#prompt", Input).value = "/retry"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.session.turns[-1].user == "do the thing"
            assert app.session.turns[-1].assistant == "**Hello** from the model"
            assert not app._busy
    asyncio.run(scenario())


def test_paste_duplicate_event_deduped(tmp_path):
    """tmux/iTerm can deliver ONE clipboard paste as TWO identical Paste events
    back-to-back; the second inside the dedup window must be dropped."""
    from textual import events

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._input.focus()
            app._input._on_paste(events.Paste("hello world"))
            app._input._on_paste(events.Paste("hello world"))  # duplicate delivery
            await pilot.pause()
            assert app._input.value == "hello world"
    asyncio.run(scenario())


def test_paste_duplicate_multiline_deduped(tmp_path):
    from textual import events

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._input.focus()
            app._input._on_paste(events.Paste("a\nb"))
            app._input._on_paste(events.Paste("a\nb"))  # duplicate delivery
            await pilot.pause()
            assert app._input.value == "[pasted 2 lines]"      # one chip, not two
            assert len(app._paste_chunks) == 1
    asyncio.run(scenario())


def test_paste_repeat_after_window_not_deduped(tmp_path):
    """A genuine second paste of the same text (outside the window) inserts."""
    from textual import events

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._input.focus()
            app._input._on_paste(events.Paste("dup"))
            app._input._last_paste = ("dup", app._input._last_paste[1] - 2.0)
            app._input._on_paste(events.Paste("dup"))
            await pilot.pause()
            assert app._input.value == "dupdup"
    asyncio.run(scenario())


# --- \r line endings (2026-07-31, session 5bb630813c21) ----------------------
# Terminals emulate keystrokes on paste, so newlines arrive as \r. The old
# `"\n" in text` check misclassified those as single-line and the stock Input
# handler kept only splitlines()[0] — a full terminal copy pasted as just its
# "Last login:" banner line.


def test_paste_cr_separated_buffers_as_chip(tmp_path):
    from textual import events

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._input.focus()
            app._input._on_paste(events.Paste("Last login: Fri\rcurl -v https://x\rHTTP/2 200"))
            await pilot.pause()
            assert "[pasted 3 lines]" in app._input.value
            # buffered text is NORMALIZED — the model sees \n
            assert app._paste_chunks == [("[pasted 3 lines]",
                                          "Last login: Fri\ncurl -v https://x\nHTTP/2 200")]
    asyncio.run(scenario())


def test_paste_crlf_separated_buffers_as_chip(tmp_path):
    from textual import events

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._input.focus()
            app._input._on_paste(events.Paste("a\r\nb"))
            await pilot.pause()
            assert "[pasted 2 lines]" in app._input.value
            assert app._paste_chunks == [("[pasted 2 lines]", "a\nb")]
    asyncio.run(scenario())


def test_paste_single_line_with_trailing_cr_inserts_inline(tmp_path):
    """One line + trailing newline is still a single-line paste — no chip."""
    from textual import events

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._input.focus()
            app._input._on_paste(events.Paste("just one line\r"))
            await pilot.pause()
            assert app._input.value == "just one line"
            assert app._paste_chunks == []
    asyncio.run(scenario())


def test_paste_cr_duplicate_delivery_deduped(tmp_path):
    """The live failure combined BOTH bugs: a \r-separated copy delivered
    twice landed as the first line concatenated with itself. Normalization +
    dedup must reduce it to ONE chip."""
    from textual import events

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._input.focus()
            app._input._on_paste(events.Paste("Last login: Fri\rreal output"))
            app._input._on_paste(events.Paste("Last login: Fri\rreal output"))
            await pilot.pause()
            assert app._input.value == "[pasted 2 lines]"
            assert len(app._paste_chunks) == 1
    asyncio.run(scenario())


def test_input_history_up_down_cycle(tmp_path):
    """Up/down recall previously submitted lines; the draft is restored when
    arrowing back past the newest entry."""
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            inp = app._input
            inp.history_remember("first")
            inp.history_remember("second")
            inp.value = "draft in progress"
            inp._history_prev()
            assert inp.value == "second"
            inp._history_prev()
            assert inp.value == "first"
            inp._history_prev()                       # at oldest: stays
            assert inp.value == "first"
            inp._history_next()
            assert inp.value == "second"
            inp._history_next()                       # past newest: draft back
            assert inp.value == "draft in progress"
            inp._history_next()                       # no-op when not navigating
            assert inp.value == "draft in progress"
    asyncio.run(scenario())


def test_input_history_recorded_at_submit_not_for_chips(tmp_path, monkeypatch):
    """Submitting a line records it; a line holding a paste chip is NOT
    recorded (its buffered text is consumed at submit)."""
    from textual import events

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            sent = []
            monkeypatch.setattr(app, "_dispatch_line",
                                lambda d, m=None: sent.append((d, m)))
            inp = app._input
            inp.focus()
            inp.value = "plain question"
            await pilot.press("enter")
            assert inp._history == ["plain question"]
            inp._on_paste(events.Paste("x\ny"))
            await pilot.press("enter")
            assert inp._history == ["plain question"]  # chip line not recorded
            assert len(sent) == 2
    asyncio.run(scenario())


def test_history_dedupes_consecutive_and_scroll_actions_safe(tmp_path):
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            inp = app._input
            inp.history_remember("same")
            inp.history_remember("same")
            assert inp._history == ["same"]
            # scroll actions must not raise regardless of content size
            app.action_scroll_up()
            app.action_scroll_down()
            app.action_scroll_line_up()
            app.action_scroll_line_down()
            app.action_scroll_home()
            app.action_scroll_end()
    asyncio.run(scenario())


def test_status_ctx_pressure_uses_server_truth(tmp_path, monkeypatch):
    """Regression (2026-07-30 "locked at 7%"): the TUI must derive ctx%
    from server-reported last_prompt_tokens like the line REPL does — the
    chars/4 estimate misses tool schemas and reads a flat ~7%. The live
    buffer must carry the same value so _end_busy's final tick can't
    clobber it with the stale estimate."""
    monkeypatch.setattr(_repl, "run_single", lambda *a, **k: _FakeResult())

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt", Input).value = "hi"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.status.num_ctx > 0
            expected = _FakeResult.last_prompt_tokens / app.status.num_ctx
            assert app.status.ctx_pressure == pytest.approx(expected)
            assert app.status.ctx_pressure != pytest.approx(
                _FakeResult.final_context_pressure)
            assert app._ctx_pressure == pytest.approx(expected)
    asyncio.run(scenario())
