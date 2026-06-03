"""Full-screen Textual TUI for `luxe chat` (chat.sdd).

Layout (Claude-CLI style): a scrollable `RichLog` transcript that grows, a
1-line status bar docked at the bottom, and an input docked below it; a transient
`#generating` line shows live activity during a turn. The blocking turn runs on a
Textual thread worker; the synchronous run_single callbacks coalesce on the
worker and a UI-thread timer renders them (never per-token marshaling).

Reuses the UI-agnostic core (`prepare_turn`/`finalize_turn`), `commands.dispatch`,
`status.fields/fit/to_rich_text`, `theme`, and `build_final_renderable`. The line
REPL (`repl.run_chat_repl`) remains the non-TTY / textual-absent fallback.
"""

from __future__ import annotations

import threading
import time
from collections import Counter

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, RichLog, Static

from luxe.chat import commands as cmd
from luxe.chat import repl as _repl
from luxe.chat import status as status_mod
from luxe.chat.render import (
    ChatCancelled,
    build_final_renderable,
    format_tool_call,
    format_tool_call_verbose,
    raise_if_cancelled,
    rainbow_banner,
    render_footer_text,
)
from luxe.chat.render import CancelToken
from luxe.chat.session import CTX_SUGGEST_PRESSURE, ChatSession, next_tier_up
from luxe.chat.status import StatusState
from luxe.memory import session as session_store

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class PromptScreen(ModalScreen[str]):
    """A small modal that asks a question and returns the typed answer. Used by
    the `prompt_user` seam (/plan choice, gitkit clone URL, compare vote) when a
    worker thread needs input. Escape dismisses with `default`."""

    def __init__(self, question: str, default: str = "") -> None:
        super().__init__()
        self._question = question
        self._default = default

    def compose(self) -> ComposeResult:
        yield Static(self._question, id="prompt_q")
        yield Input(id="prompt_input")

    def on_mount(self) -> None:
        self.query_one("#prompt_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value or self._default)

    def key_escape(self) -> None:
        self.dismiss(self._default)


class StatusBar(Static):
    """Bottom status bar — renders `status.fields()` fitted to width."""

    def __init__(self, app_ref: "ChatApp") -> None:
        super().__init__(id="status")
        self._app = app_ref

    def render(self):
        a = self._app
        try:
            segs = status_mod.fields(a.session, a.slots, a.repo_path, a.status)
            return status_mod.to_rich_text(status_mod.fit(segs, self.size.width or 80))
        except Exception:
            return Text("")


class ChatApp(App):
    CSS_PATH = "tui.css"
    BINDINGS = [
        Binding("escape", "cancel", "Cancel turn", show=True),
        Binding("ctrl+c", "cancel", "Cancel turn", show=False, priority=True),
        Binding("ctrl+q", "quit_app", "Quit", show=True),
    ]

    def __init__(self, cfg, repo_path, languages, *, session, slots, infer,
                 keep_loaded=False):
        super().__init__()
        self.cfg = cfg
        self.repo_path = repo_path
        self.languages = languages
        self.session: ChatSession = session
        self.slots = slots
        self.infer = infer
        self.keep_loaded = keep_loaded
        self.cancel = CancelToken()
        self.status = StatusState(opened_at=time.time(),
                                  num_ctx=slots.role_for("chat").num_ctx)
        self._busy = False
        self._worker_thread: threading.Thread | None = None
        # live-turn coalescing buffers (written on the worker, read by the timer)
        self._stream = ""
        self._tool_counts: Counter = Counter()
        self._gen_started = 0.0
        self._timer = None
        self.ctx: cmd.CommandContext | None = None
        # Cached widget refs (set in on_mount). We hold references rather than
        # query_one() each tick because `App.query_one` only searches the ACTIVE
        # screen — once a PromptScreen modal is pushed the base-screen widgets are
        # no longer found, which would crash the timer. A held ref stays valid.
        self._gen: Static | None = None
        self._status_bar: StatusBar | None = None
        self._transcript: RichLog | None = None
        self._input: Input | None = None

    # -- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield RichLog(id="transcript", wrap=True, markup=True, highlight=False,
                      max_lines=5000, auto_scroll=True)
        # Bottom block stacks deterministically: generating, status, then input.
        with Vertical(id="bottom"):
            yield Static("", id="generating")
            yield StatusBar(self)
            yield Input(id="prompt", placeholder="message luxe — /help for commands")

    def on_mount(self) -> None:
        from luxe.buildinfo import build_status_hint, version_parts

        log = self._log()
        sha, dirty = version_parts()
        state = "[yellow](dirty)[/]" if dirty else "[dim green](clean)[/]"
        log.write(rainbow_banner("luxe")
                  + f"  [dim]· version {sha}[/] {state} "
                  + f"[dim]· session {self.session.session_id} · /help[/]")
        hint = build_status_hint()
        if hint:
            log.write(f"[yellow][hint][/] [dim]{hint}[/]")
        # Cache long-lived widget refs (see __init__): used directly so the timer
        # and writes survive a modal screen being on top.
        self._gen = self.query_one("#generating", Static)
        self._status_bar = self.query_one("#status", StatusBar)
        self._transcript = self.query_one("#transcript", RichLog)
        self._input = self.query_one("#prompt", Input)
        self._gen.display = False
        self.ctx = cmd.CommandContext(
            console=LogConsole(self),
            session=self.session,
            slots=self.slots,
            on_git_analysis=self._git_hook,
            on_compare=self._compare_hook,
            on_compare_review=self._compare_review_hook,
        )
        self._input.focus()

    # -- helpers ------------------------------------------------------------
    def _log(self) -> RichLog:
        # cached after on_mount; fall back to a query before then.
        return self._transcript or self.query_one("#transcript", RichLog)

    def write(self, renderable) -> None:
        """Thread-safe write into the transcript (callable from any thread)."""
        log = self._log()
        if threading.current_thread() is threading.main_thread():
            log.write(renderable)
        else:
            self.call_from_thread(log.write, renderable)

    def is_worker_thread(self) -> bool:
        return self._worker_thread is threading.current_thread()

    def refresh_status(self) -> None:
        try:
            if self._status_bar is not None:
                self._status_bar.refresh()
        except Exception:
            pass

    # -- input --------------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt":
            return
        line = (event.value or "").strip()
        event.input.value = ""
        if not line:
            return
        if self._busy:
            self.write("[yellow]· busy — wait for the current turn to finish "
                       "(esc to cancel)[/]")
            return
        self.write(Text(f"❯ {line}", style="bold"))
        if cmd.is_command(line):
            self._run_command(line)
        else:
            self._run_turn(line)

    def on_key(self, event) -> None:
        # ctrl+d on an empty prompt quits (Claude-CLI convention).
        if event.key == "ctrl+d" and self._input is not None and not self._input.value:
            event.stop()
            self.action_quit_app()

    # -- actions ------------------------------------------------------------
    def action_cancel(self) -> None:
        if self._busy:
            self.cancel.requested = True
            self.write("[yellow]· cancelling…[/]")
        # If a modal prompt is open, dismiss it with its default so a blocked
        # worker can unwind.
        if isinstance(self.screen, PromptScreen):
            self.screen.dismiss(self.screen._default)

    def action_quit_app(self) -> None:
        if not self.keep_loaded:
            try:
                self.slots.unload_all()
            except Exception:
                pass
        self.exit()

    # -- turn worker --------------------------------------------------------
    def _begin_busy(self) -> None:
        self._busy = True
        self._gen.display = True
        self._input.disabled = True
        self._timer = self.set_interval(0.1, self._tick)

    def _reset_gen(self) -> None:
        """Reset the per-turn live buffers (called at the start of each turn so
        goal-loop rounds restart the spinner/preview)."""
        self._stream = ""
        self._tool_counts = Counter()
        self._gen_started = time.time()

    def _end_busy(self) -> None:
        self._tick()  # final frame so the last ~100ms isn't clipped
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._gen is not None:
            self._gen.display = False
        if self._input is not None:
            self._input.disabled = False
            self._input.focus()
        self._busy = False
        self.refresh_status()

    def _tick(self) -> None:
        # Skip painting while a modal (PromptScreen) is up — its widgets aren't on
        # the base screen and the spinner would be hidden anyway.
        if self._gen is None or isinstance(self.screen, PromptScreen):
            return
        elapsed = time.time() - self._gen_started if self._gen_started else 0.0
        frame = _SPINNER[int(elapsed * 10) % len(_SPINNER)]
        tools = " · ".join(f"{n} ({c})" for n, c in self._tool_counts.most_common(4))
        head = f"{frame} {elapsed:4.1f}s" + (f" · {tools}" if tools else "")
        tail = self._stream[-200:].replace("\n", " ")
        try:
            self._gen.update(Text(head, style="cyan") + Text(f"  {tail}", style="dim"))
        except Exception:
            return
        self.refresh_status()

    def _execute_turn_blocking(self, message: str, *, plan_mode: bool = False):
        """Run ONE turn on the current worker thread, rendering into the RichLog.
        Owns no busy/timer state (the outer worker does) so it's reusable by the
        single-turn worker AND the goal/plan loops (which call it per round via
        `_tui_run_turn`)."""
        self.call_from_thread(self._reset_gen)
        prep = _repl.prepare_turn(message, self.session, self.slots, self.cfg,
                                  self.languages, self.infer, plan_mode=plan_mode)
        self.call_from_thread(
            self.write, Text(f"slot: {prep.slot} · model: {prep.model}", style="dim"))

        def _on_event(tc):
            self._tool_counts[getattr(tc, "name", "?")] += 1
            prep.note_tool(tc)
            if self.session.verbose_level in ("diff", "full"):
                self.call_from_thread(
                    self.write, format_tool_call_verbose(tc, self.session.verbose_level))
            else:
                self.call_from_thread(self.write, format_tool_call(tc))
            raise_if_cancelled(self.cancel)

        def _on_token(delta):
            raise_if_cancelled(self.cancel)
            self._stream += delta

        started = time.time()
        interrupted = False
        result = None
        try:
            result = prep.call(_on_event, _on_token, lambda p: None)
        except (ChatCancelled, KeyboardInterrupt):
            interrupted = True
        ended = time.time()
        outcome = _repl.finalize_turn(self.session, prep, result,
                                      interrupted=interrupted, message=message,
                                      started_at=started, ended_at=ended)
        self.call_from_thread(self._render_outcome, outcome, prep, interrupted)
        return outcome

    def _tui_run_turn(self, message, session=None, slots=None, cfg=None,
                      languages=None, console=None, cancel=None, infer=None,
                      status=None, plan_mode=False):
        """Adapter matching `_run_turn`'s signature so `repl._run_goal_loop` /
        `_run_plan` drive TUI turns (uses the app's own session/slots/etc)."""
        return self._execute_turn_blocking(message, plan_mode=plan_mode)

    @work(thread=True, exclusive=True, group="turn")
    def _run_turn(self, message: str) -> None:
        self.cancel.reset()
        self.call_from_thread(self._begin_busy)
        try:
            self._execute_turn_blocking(message)
        finally:
            self.call_from_thread(self._end_busy)

    def _render_outcome(self, outcome, prep, interrupted) -> None:
        log = self._log()
        if interrupted:
            log.write("[yellow]· interrupted — partial turn saved[/]")
            return
        result = outcome.result
        if result is None:
            return
        mode = ("full" if self.session.verbose_level == "full"
                else "compact" if self.session.compact else "truncated")
        log.write(build_final_renderable(outcome.final_text, mode=mode))
        log.write(Text(render_footer_text(prep.slot, prep.model, result), style="dim"))
        # update persistent status from the completed turn
        s = self.status
        s.slot, s.model = prep.slot, prep.model
        s.wall_s = result.wall_s
        s.tok_per_s = (result.completion_tokens / result.wall_s
                       if result.wall_s > 0 else 0.0)
        s.ctx_pressure = result.final_context_pressure
        s.num_ctx = outcome.num_ctx
        s.prompt_tokens = result.prompt_tokens
        s.steps = result.steps
        s.has_turn = True
        if result.peak_context_pressure >= CTX_SUGGEST_PRESSURE:
            nxt = next_tier_up(outcome.num_ctx, prep.ctx_ceiling)
            if nxt:
                log.write(f"[dim]· context pressure {result.peak_context_pressure:.0%} "
                          f"— `/ctx {nxt[0]}` gives more headroom[/]")

    # -- command worker -----------------------------------------------------
    @work(thread=True, exclusive=True, group="turn")
    def _run_command(self, line: str) -> None:
        self.cancel.reset()
        self.call_from_thread(self._begin_busy)
        try:
            res = cmd.dispatch(line, self.ctx)
            if getattr(res, "exit", False):
                self.call_from_thread(self.action_quit_app)
                return
            console = LogConsole(self)
            # /plan and /goal set session flags the line loop would act on; here we
            # run their routines on this worker, driving TUI turns + a modal prompt.
            if self.session.plan_pending:
                _repl._run_plan(self.session, self.slots, self.cfg, self.languages,
                                console, self.cancel, self.infer, None,
                                run_turn=self._tui_run_turn, reader=self.prompt_user)
            if self.session.goal_active:
                _repl._run_goal_loop(self.session, self.slots, self.cfg, self.languages,
                                     console, self.cancel, self.infer, None,
                                     run_turn=self._tui_run_turn)
        finally:
            self.call_from_thread(self._end_busy)

    # -- prompt_user seam ---------------------------------------------------
    def prompt_user(self, question: str, default: str = "") -> str:
        """Block the calling WORKER for an answer via a modal. Must be called from
        a worker thread, not the UI thread (else it deadlocks)."""
        assert not (threading.current_thread() is threading.main_thread()), \
            "prompt_user must be called from a worker thread"
        return self.call_from_thread(self.push_screen_wait, PromptScreen(question, default))

    # -- feature hooks (run on the worker; reader/console route to the TUI) --
    def _git_hook(self, kind: str) -> None:
        from luxe.gitkit import run_git_report
        run_git_report(kind, cfg=self.cfg, repo_path=self.session.repo_path,
                       console=LogConsole(self), reader=self._reader, save=True,
                       verbose=(self.session.verbose_level == "full"),
                       expected_head=self.session.index_head)

    def _compare_hook(self, task: str) -> None:
        try:
            from luxe.compare.run_pair import interactive_compare
        except Exception:
            self.write("[yellow]compare unavailable.[/]")
            return
        interactive_compare(task, self.cfg, self.session.repo_path, self.languages,
                            console=LogConsole(self), reader=self._reader)

    def _compare_review_hook(self, compare_id: str) -> None:
        try:
            from luxe.compare.store import review as review_compare
        except Exception:
            self.write("[yellow]compare review unavailable.[/]")
            return
        review_compare(compare_id, console=LogConsole(self))

    def _reader(self, prompt: str) -> str:
        return self.prompt_user(prompt)


class _NullStatus:
    """Context-manager stand-in for `console.status(...)` inside the TUI; routes
    `.update()` to the transient #generating line."""
    def __init__(self, app: ChatApp):
        self._app = app

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, text):
        self._app.write(Text(str(text), style="dim"))


class LogConsole:
    """A console-compatible shim whose output lands in the TUI transcript. Covers
    the surface `commands.dispatch` / gitkit / compare touch (`print`, `input`,
    `status`, `is_terminal`, `width`/`size`); unknown attrs degrade gracefully."""

    is_terminal = True

    def __init__(self, app: ChatApp):
        self._app = app

    @property
    def width(self) -> int:
        try:
            return self._app.size.width
        except Exception:
            return 100

    @property
    def size(self):
        return self._app.size

    def print(self, *args, **kwargs) -> None:
        if not args:
            self._app.write("")
            return
        for a in args:
            self._app.write(a)

    def input(self, prompt: str = "") -> str:
        return self._app.prompt_user(str(prompt))

    def status(self, *args, **kwargs):
        return _NullStatus(self._app)

    def rule(self, *args, **kwargs) -> None:
        self._app.write(Text("─" * 40, style="dim"))

    def __getattr__(self, name):  # graceful no-op for any other console method
        def _noop(*a, **k):
            return None
        return _noop


def run_chat_app(cfg, repo_path, languages, *, keep_loaded=False,
                 resume_session_id=None, dev_mode=False, startup_verbose=None,
                 startup_show_reasoning=False, startup_no_terse=False,
                 startup_debug=False, startup_compact=False, theme_name=None,
                 infer_task_type=None) -> None:
    """Entry point: build the session + app and run it. Mirrors run_chat_repl's
    startup-flag handling so the two front-ends are interchangeable."""
    from luxe.chat.slots import SlotManager
    from luxe.cli import _infer_task_type
    from luxe.gitkit.health import current_head
    from luxe.memory import project as project_mem

    if theme_name:
        from luxe.chat import theme as theme_mod
        theme_mod.set_palette(theme_name)

    infer = infer_task_type or _infer_task_type
    slots = SlotManager(cfg, on_status=lambda m: None)
    session = ChatSession(
        repo_path=repo_path,
        project_hash=project_mem.project_hash(repo_path) if repo_path else "",
        languages=languages,
    )
    if dev_mode:
        session.write_enabled = True
        session.unrestricted_bash = True
    if startup_debug:
        session.verbose_level = "full"
        session.show_reasoning = True
    elif startup_verbose:
        session.verbose_level = startup_verbose
    if startup_show_reasoning:
        session.show_reasoning = True
    if startup_no_terse:
        session.terse = False
    if startup_compact:
        session.compact = True

    meta = session_store.new_session(repo_path=repo_path,
                                     project_hash=session.project_hash,
                                     slot_models=slots.slot_models())
    session.session_id = meta.session_id
    session.index_head = current_head(repo_path) if repo_path else ""

    app = ChatApp(cfg, repo_path, languages, session=session, slots=slots,
                  infer=infer, keep_loaded=keep_loaded)
    try:
        app.run()
    finally:
        if not keep_loaded:
            try:
                slots.unload_all()
            except Exception:
                pass
