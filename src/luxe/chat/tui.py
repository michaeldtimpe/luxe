"""Full-screen Textual TUI for `luxe chat` (chat.sdd).

Layout (Claude-CLI style): a scrollable `RichLog` transcript that grows, a
1-line status bar docked at the bottom, and an input docked below it; a transient
`#generating` line shows live activity during a turn. The blocking turn runs on a
Textual thread worker; the synchronous run_single callbacks coalesce on the
worker and a UI-thread timer renders them (never per-token marshaling).

Everything that is not drawing or input — the session, the turn pipeline,
containment, /plan + /goal, teardown — is `controller.ChatController`; this
module renders it (`TuiSink`) and owns the Textual app. Reuses
`commands.dispatch`, `status.fields/fit/to_rich_text`, `theme`, and
`build_final_renderable`. The line REPL (`repl.run_chat_repl`) remains the
non-TTY / textual-absent fallback.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from contextlib import contextmanager

from rich.errors import MarkupError
from rich.markup import escape as _escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, RichLog, Static

from luxe.chat import commands as cmd
from luxe.chat import cost as cost_mod
from luxe.chat import status as status_mod
from luxe.chat.controller import (
    ChatController,
    TurnHooks,
    apply_project_summary,
    banner_markup,
    build_hint_markup,
    ephemeral_notice_markup,
    initial_status,
    model_origin_notice,
)
from luxe.chat.render import (
    build_final_renderable,
    format_tool_call_verbose,
    render_footer_text,
)
from luxe.chat.session import ChatSession
from luxe.chat.status import StatusState
from luxe.chat.turn import TurnOutcome, TurnPrep, attachments_kept_note

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

logger = logging.getLogger(__name__)


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


class ChatInput(Input):
    """The prompt input: paste-aware with input history.

    Paste: Textual's stock Input keeps only the FIRST line of a paste —
    silently destructive for multi-line pastes (code, logs). Here:
    single-line pastes insert normally; multi-line pastes buffer on the app
    and show a compact "[pasted N lines]" chip in the input, which is
    expanded back to the full text at submit time.

    Line endings (2026-07-31): terminals emulate KEYSTROKES on paste, so
    newlines arrive as \r (CR), not \n — `"\n" in text` misclassified every
    \r-separated paste as single-line and fell through to the stock
    first-line-only handler (session 5bb630813c21: a full terminal copy
    pasted as just its "Last login:" banner line). All \r\n / \r are
    normalized to \n on arrival; multi-line detection uses splitlines().

    Some terminal stacks (tmux/iTerm passthrough) deliver ONE clipboard
    paste as TWO identical Paste events back-to-back, so an identical paste
    inside the dedup window is dropped. The window is 1.5s — large pastes
    can take >0.35s to parse, which let the duplicate through (same
    session: the banner line landed twice, concatenated). A deliberate
    re-paste of identical text on the same input line within 1.5s is not a
    real workflow; re-paste after submit is unaffected.

    History: up/down cycle previously submitted lines (readline-style); the
    in-progress draft is kept and restored when you arrow back past the
    newest entry. Lines that contained paste chips are not recorded — their
    buffered text is consumed at submit, so recalling the chip would send
    the literal "[pasted N lines]" string.

    Clearing: `clear_input()` is the ctrl+c path (see `ChatApp.action_interrupt`)
    — it empties the box AND puts history navigation back at rest, because a
    line the user just threw away is not a draft to arrow back to.
    """

    _PASTE_DEDUP_WINDOW_S = 1.5

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_paste: tuple[str, float] = ("", 0.0)
        self._history: list[str] = []
        self._hist_idx: int | None = None
        self._draft = ""

    def _on_paste(self, event) -> None:
        # Normalize FIRST: paste is emulated typing, so terminals deliver
        # newlines as \r — the raw text of a multi-line paste often contains
        # no \n at all (see class docstring).
        raw = getattr(event, "text", "") or ""
        text = raw.replace("\r\n", "\n").replace("\r", "\n")
        event.stop()
        event.prevent_default()
        now = time.monotonic()
        last_text, last_at = self._last_paste
        self._last_paste = (text, now)
        if text and text == last_text and (now - last_at) < self._PASTE_DEDUP_WINDOW_S:
            return
        lines = text.splitlines()
        if len(lines) > 1:
            self.app.buffer_paste(text)
            return
        # Single line (possibly with a trailing newline stripped by
        # splitlines) — insert at the cursor, replacing any selection,
        # mirroring the stock Input handler.
        line = lines[0] if lines else ""
        if not line:
            return
        selection = self.selection
        if selection.is_empty:
            self.insert_text_at_cursor(line)
        else:
            self.replace(line, *selection)

    def clear_input(self) -> None:
        """Empty the box and stop history navigation. Setting `.value` alone
        would leave `_hist_idx` mid-recall, so the next ↓ would repopulate the
        line the user just cleared."""
        self.value = ""
        self.cursor_position = 0
        self._hist_idx = None
        self._draft = ""

    # -- input history -------------------------------------------------------
    def history_remember(self, line: str) -> None:
        if line and (not self._history or self._history[-1] != line):
            self._history.append(line)
        self._hist_idx = None
        self._draft = ""

    def _history_prev(self) -> None:
        if not self._history:
            return
        if self._hist_idx is None:
            self._draft = self.value
            self._hist_idx = len(self._history) - 1
        elif self._hist_idx > 0:
            self._hist_idx -= 1
        self.value = self._history[self._hist_idx]
        self.cursor_position = len(self.value)

    def _history_next(self) -> None:
        if self._hist_idx is None:
            return
        if self._hist_idx < len(self._history) - 1:
            self._hist_idx += 1
            self.value = self._history[self._hist_idx]
        else:
            self._hist_idx = None
            self.value = self._draft
        self.cursor_position = len(self.value)

    def on_key(self, event) -> None:
        if event.key == "up":
            event.stop()
            event.prevent_default()
            self._history_prev()
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            self._history_next()


class StatusBar(Static):
    """Bottom status bar — renders `status.fields()` fitted to width."""

    def __init__(self, app_ref: "ChatApp") -> None:
        super().__init__(id="status")
        self._app = app_ref

    def render(self):
        a = self._app
        try:
            # git state refreshes on a background thread (a render must never
            # spawn `git`); when it lands, the bar repaints itself.
            segs = status_mod.fields(a.session, a.slots, a.session.repo_path,
                                     a.status, on_git_update=a.git_updated)
            return status_mod.to_rich_text(status_mod.fit(segs, self.size.width or 80))
        except Exception:
            return Text("")


class ChatApp(App):
    CSS_PATH = "tui.css"
    BINDINGS = [
        Binding("escape", "cancel", "Cancel turn", show=True),
        Binding("ctrl+c", "interrupt", "Clear input", show=False, priority=True),
        Binding("ctrl+q", "quit_app", "Quit", show=True),
        # Scroll the transcript even while the input holds focus. The TUI runs
        # on the alternate screen, so the TERMINAL/tmux scrollback never sees
        # transcript history — these keys (plus mouse wheel) are the scrollback.
        Binding("pageup", "scroll_up", "Scroll up", show=False),
        Binding("pagedown", "scroll_down", "Scroll down", show=False),
        Binding("shift+up", "scroll_line_up", "Scroll line up", show=False),
        Binding("shift+down", "scroll_line_down", "Scroll line down", show=False),
        Binding("home", "scroll_home", "To top", show=False),
        Binding("end", "scroll_end", "To bottom", show=False),
    ]

    def __init__(self, cfg, repo_path=None, languages=None, *, session=None,
                 slots=None, infer=None, keep_loaded=False,
                 resume_session_id=None, on_project=None, dbglog=None,
                 controller: ChatController | None = None):
        """`controller` is the session (run_chat_app builds and starts one).
        Without it — tests, embedders — one is made around the given
        `session`/`slots`/`infer`, with the status bar seeded from the window
        the FIRST turn will use (`controller.initial_status`). The live
        project is the session's: `repo_path`/`languages` are read from it."""
        super().__init__()
        if controller is None:
            controller = ChatController(
                cfg, session=session, slots=slots, infer=infer,
                status=initial_status(slots), keep_loaded=keep_loaded,
                dbglog=dbglog, log=logger)
        self.controller = controller
        self.sink = TuiSink(self)
        self._resume_id = resume_session_id
        self._on_project = on_project
        self._busy = False
        # Set by `action_quit_app`. A worker still unwinding after the app is
        # gone gets `RuntimeError('App is not running')` from every
        # `call_from_thread`; this flag lets it skip the UI work — and the
        # kind="error" crash record that RuntimeError used to produce.
        self._exiting = False
        # live-turn coalescing buffers (written on the worker, read by the
        # timer). The streamed chunks themselves are the controller's
        # (`_stream_parts`); the timer only ever shows the bounded tail.
        self._stream_tail = ""
        self._tool_counts: Counter = Counter()
        self._gen_started = 0.0
        self._ctx_pressure = 0.0          # live context pressure (on_progress)
        self._activity: str | None = None  # gitkit/compare set this for the live line
        self._running_cmd = ""             # bash command currently executing
        self._reasoning_since = 0.0        # epoch of the current thinking burst
        self._timer = None
        self._queue: list[tuple[str, str]] = []  # type-ahead: (display, message) mid-run
        self._paste_chunks: list[tuple[str, str]] = []  # (chip, full text) pending
        self._session_in = 0               # session cumulative prompt tokens
        self._session_out = 0              # session cumulative completion tokens
        # Cached widget refs (set in on_mount). We hold references rather than
        # query_one() each tick because `App.query_one` only searches the ACTIVE
        # screen — once a PromptScreen modal is pushed the base-screen widgets are
        # no longer found, which would crash the timer. A held ref stays valid.
        self._gen: Static | None = None
        self._status_bar: StatusBar | None = None
        self._transcript: RichLog | None = None
        self._input: Input | None = None

    # -- the session, as the controller holds it ----------------------------
    @property
    def cfg(self):
        return self.controller.cfg

    @property
    def session(self) -> ChatSession:
        return self.controller.session

    @property
    def slots(self):
        return self.controller.slots

    @property
    def infer(self):
        return self.controller.infer

    @property
    def keep_loaded(self) -> bool:
        return self.controller.keep_loaded

    @property
    def cancel(self):
        return self.controller.cancel

    @property
    def status(self) -> StatusState:
        return self.controller.status

    @property
    def ctx(self) -> cmd.CommandContext | None:
        return self.controller.ctx

    @property
    def repo_path(self) -> str:
        return self.controller.repo_path

    @property
    def languages(self) -> frozenset:
        return self.controller.languages

    @property
    def _dbglog(self):
        # `/ephemeral` must detach this before removing the session directory
        # the handler has debug.log open inside.
        return self.controller.dbglog

    @property
    def _stream_parts(self) -> list[str]:
        return self.controller.stream_parts

    # -- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield RichLog(id="transcript", wrap=True, markup=True, highlight=False,
                      max_lines=10000, auto_scroll=True)
        # Bottom block stacks deterministically: generating, status, then input.
        with Vertical(id="bottom"):
            yield Static("", id="generating")
            yield StatusBar(self)
            yield ChatInput(id="prompt", placeholder="message luxe — /help for commands")

    def on_mount(self) -> None:
        log = self._log()
        # Shared banner format with the line REPL (controller.banner_markup).
        log.write(banner_markup(self.session.session_id))
        # The alt-screen TUI is invisible to terminal/tmux scrollback — say so
        # once, with the keys that ARE the scrollback (plus ↑/↓ input history).
        log.write("[dim]· scroll: PgUp/PgDn · shift+↑/↓ · Home/End · mouse "
                  "wheel (terminal scrollback can't see the TUI) · "
                  "↑/↓ recall your prior inputs · ctrl+c clears the input "
                  "(esc interrupts a turn)[/]")
        if (eph_notice := ephemeral_notice_markup()):
            log.write(eph_notice)
        if (hint := build_hint_markup()):
            log.write(hint)
        # Model provenance (local disk / network volume / remote host), stated
        # once — same notice the line REPL prints.
        log.write(model_origin_notice(self.slots, self.status))
        # Route SlotManager notices (weight swaps, auto-degrade) into the
        # transcript. run_chat_app constructs slots before the app exists, so
        # the binding lands here; self.write is thread-safe, and the degrade
        # announcement MUST be visible in the TUI, not just the line REPL.
        self.slots._on_status = lambda m: self.write(Text(f"· {m}", style="dim"))
        # Cache long-lived widget refs (see __init__): used directly so the timer
        # and writes survive a modal screen being on top.
        self._gen = self.query_one("#generating", Static)
        self._status_bar = self.query_one("#status", StatusBar)
        self._transcript = self.query_one("#transcript", RichLog)
        self._input = self.query_one("#prompt", Input)
        self._gen.display = False
        self.controller.ctx = cmd.CommandContext(
            console=LogConsole(self),
            session=self.session,
            slots=self.slots,
            on_resume=self._resume_hook,
            on_git_analysis=self._git_hook,
            on_compare=self._compare_hook,
            on_compare_review=self._compare_review_hook,
            on_project=self._project_hook,
            status=self.status,
            session_log=self._dbglog,
            run_external=self.run_external,
        )
        # `--resume <id>`: replay the prior transcript into the RichLog and seed
        # the live session's turns before the first prompt (chat.sdd — resume no
        # longer forces the line REPL).
        if self._resume_id:
            self._resume_hook(self._resume_id)
        self._input.focus()

    # -- helpers ------------------------------------------------------------
    def _log(self) -> RichLog:
        # cached after on_mount; fall back to a query before then.
        return self._transcript or self.query_one("#transcript", RichLog)

    def write(self, renderable) -> None:
        """Thread-safe write into the transcript (callable from any thread).

        A str is Rich markup. Every site that interpolates user, model, or
        exception text escapes it (or passes a `Text`), but a single miss used
        to be FATAL here: RichLog parses markup inside `write`, a stray `[/x]`
        raised MarkupError on the UI thread, and Textual tore the app down —
        conversation and all. So a string that will not parse is shown
        literally instead of taking the session with it."""
        if isinstance(renderable, str):
            try:
                renderable = Text.from_markup(renderable)
            except MarkupError:
                renderable = Text(renderable)
        log = self._log()
        if threading.current_thread() is threading.main_thread():
            log.write(renderable)
        else:
            self._call_ui(log.write, renderable)

    def _call_ui(self, fn, *args):
        """`call_from_thread`, except that once the app is exiting a UI that
        no longer exists is not an error — the call is dropped (None)."""
        if self._exiting:
            try:
                return self.call_from_thread(fn, *args)
            except RuntimeError:
                return None
        return self.call_from_thread(fn, *args)

    def git_updated(self) -> None:
        """Background git refresh finished (any thread): repaint the bar."""
        try:
            if threading.current_thread() is threading.main_thread():
                self.refresh_status()
            else:
                self.call_from_thread(self.refresh_status)
        except Exception:
            pass

    def refresh_status(self) -> None:
        try:
            if self._status_bar is not None:
                self._status_bar.refresh()
        except Exception:
            pass

    # -- input --------------------------------------------------------------
    def buffer_paste(self, text: str) -> None:
        """Multi-line paste (from ChatInput): keep the full text aside and show
        a compact chip in the input; `_expand_pastes` restores it at submit."""
        n = len(text.splitlines())
        chip = f"[pasted {n} lines]"
        self._paste_chunks.append((chip, text))
        if self._input is not None:
            self._input.insert_text_at_cursor(chip)

    def _expand_pastes(self, line: str) -> str:
        """Replace each pending paste chip with its buffered text (in order);
        chips the user deleted get their text appended so nothing is lost."""
        for chip, text in self._paste_chunks:
            if chip in line:
                line = line.replace(chip, text, 1)
            else:
                line = f"{line}\n\n{text}" if line else text
        self._paste_chunks = []
        return line

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt":
            return
        line = (event.value or "").strip()
        event.input.value = ""
        if not line and not self._paste_chunks:
            return
        # The transcript shows the compact chip form; the model gets the
        # expanded text.
        display = line
        had_chunks = bool(self._paste_chunks)
        message = self._expand_pastes(line) if self._paste_chunks else line
        if not had_chunks and isinstance(event.input, ChatInput):
            event.input.history_remember(line)
        if not message:
            return
        if self._busy:
            if message.strip().lower() == "/goal stop" and self.session.goal_active:
                # Queued, `/goal stop` would run only AFTER the goal it means
                # to stop. The supervisor checks `goal_active` between rounds,
                # so flipping it now ends the goal at the end of this round
                # (esc still cancels the round itself).
                self.session.goal_active = False
                self.write("[yellow]· goal will stop after the current round "
                           "(esc interrupts it now)[/]")
                return
            # Type-ahead: queue it and run after the current task (esc cancels the
            # current one). One run_single per turn is preserved.
            self._queue.append((display, message))
            self.write(f"[yellow]· queued[/] [dim]{_escape(display)}[/]")
            return
        self._dispatch_line(display, message)

    def _dispatch_line(self, display: str, message: str | None = None) -> None:
        message = display if message is None else message
        self.write(Text(f"❯ {display}", style="bold"))
        if cmd.is_command(message):
            self._run_command(message)
        else:
            self._run_turn(message)

    def _maybe_drain(self) -> None:
        """Run the next queued message once the current task has finished."""
        if self._busy or not self._queue:
            return
        display, message = self._queue.pop(0)
        self._dispatch_line(display, message)

    def on_key(self, event) -> None:
        # ctrl+d on an empty prompt quits (Claude-CLI convention).
        if event.key == "ctrl+d" and self._input is not None and not self._input.value:
            event.stop()
            self.action_quit_app()

    # -- scroll actions (work while the input has focus) --------------------
    def action_scroll_up(self) -> None:
        if self._transcript is not None:
            self._transcript.scroll_page_up()

    def action_scroll_down(self) -> None:
        if self._transcript is not None:
            self._transcript.scroll_page_down()

    def action_scroll_line_up(self) -> None:
        if self._transcript is not None:
            self._transcript.scroll_up()

    def action_scroll_line_down(self) -> None:
        if self._transcript is not None:
            self._transcript.scroll_down()

    def action_scroll_home(self) -> None:
        if self._transcript is not None:
            self._transcript.scroll_home()

    def action_scroll_end(self) -> None:
        if self._transcript is not None:
            self._transcript.scroll_end()

    # -- actions ------------------------------------------------------------
    def action_interrupt(self) -> None:
        """ctrl+c, Claude-CLI style: cancel a running turn, else clear the input.

        Cancel keeps precedence while a turn is running (or a modal is up), so
        the key means exactly what it always meant wherever it already meant
        something; the clear fills the idle case, which used to be a silent
        no-op. It NEVER quits — ctrl+d on an empty prompt, ctrl+q and `/quit`
        remain the only exits (deliberate divergence from Claude CLI's
        double-tap: a mis-aimed ctrl+c must not be able to end a session).
        """
        if self._busy or isinstance(self.screen, PromptScreen):
            self.action_cancel()
            return
        inp = self._input
        if inp is None or (not inp.value and not self._paste_chunks):
            return
        # A buffered multi-line paste lives on the APP, not in the input, so
        # dropping the chip has to drop its text too — otherwise the next
        # submit resurrects a paste the user just cleared (`_expand_pastes`
        # appends chunks whose chip is gone).
        self._paste_chunks = []
        if isinstance(inp, ChatInput):
            inp.clear_input()
        else:
            inp.value = ""

    def action_cancel(self) -> None:
        if self._busy:
            self.cancel.requested = True
            self.write("[yellow]· cancelling…[/]")
        # If a modal prompt is open, dismiss it with its default so a blocked
        # worker can unwind.
        if isinstance(self.screen, PromptScreen):
            self.screen.dismiss(self.screen._default)

    def action_quit_app(self) -> None:
        """Leave the app. The model unload is NOT done here: `run_chat_app`'s
        finally runs session notes FIRST (they need the loaded model — unloading
        here forced a full reload just to distil) and unloads after.

        Quitting mid-turn sets the cancel token: the worker thread cannot be
        killed, and without the token it kept running tools after the screen
        closed. `App.run()` still returns only once the worker has finished
        (asyncio.run waits for executor threads), and the token is polled only
        at the next streamed token or tool dispatch — so a quit during a long
        prefill waits for the first token. By the time `run_chat_app`'s finally
        runs, the turn is over, so its unload is safe."""
        self._exiting = True
        if self._busy:
            self.cancel.requested = True
            if isinstance(self.screen, PromptScreen):
                self.screen.dismiss(self.screen._default)
        self.exit()

    # -- turn worker --------------------------------------------------------
    def _begin_busy(self) -> None:
        # Input stays ENABLED (type-ahead queue) — submissions during a run are
        # queued, not blocked.
        self._busy = True
        self._gen.display = True
        self._timer = self.set_interval(0.1, self._tick)

    def _reset_gen(self) -> None:
        """Reset the per-turn live buffers (called at the start of each turn so
        goal-loop rounds restart the spinner/preview). The streamed chunks
        themselves are reset by the controller as the turn starts."""
        self._stream_tail = ""
        self._tool_counts = Counter()
        self._gen_started = time.time()
        self._ctx_pressure = 0.0
        self._activity = None
        self._running_cmd = ""
        self._reasoning_since = 0.0

    def _end_busy(self) -> None:
        self._tick()  # final frame so the last ~100ms isn't clipped
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._gen is not None:
            self._gen.display = False
        self._busy = False
        self.refresh_status()
        # Run the next queued message (if any) on the next UI tick, after this
        # worker has fully exited.
        self.set_timer(0.05, self._maybe_drain)

    def _tick(self) -> None:
        # Skip painting while a modal (PromptScreen) is up — its widgets aren't on
        # the base screen and the spinner would be hidden anyway.
        if self._gen is None or isinstance(self.screen, PromptScreen):
            return
        # Live context pressure (Item 2): folded in from on_progress so the status
        # bar's ctx% ticks during the turn, not only at the end.
        if self._ctx_pressure:
            self.status.ctx_pressure = self._ctx_pressure
            self.status.has_turn = True
        elapsed = time.time() - self._gen_started if self._gen_started else 0.0
        frame = _SPINNER[int(elapsed * 10) % len(_SPINNER)]
        if self._activity is not None:
            # gitkit/compare drive a phased activity string (coalesced, no flood).
            body = f"{self._activity} · {elapsed:4.1f}s · esc to interrupt"
        else:
            tools = " · ".join(f"{n} ({c})" for n, c in self._tool_counts.most_common(4))
            # The command currently EXECUTING (set at bash dispatch, cleared on
            # completion) — a hung command names itself instead of the line
            # showing only counts of finished calls.
            running = self._running_cmd
            if running:
                tools = (tools + " · " if tools else "") + f"$ {running[:80]}"
            # Reasoning channel: counts only, never the text (chat.sdd). A
            # thinking model can be silent for minutes before its first
            # content token — measured at 10.5 min with nothing on screen.
            if self._reasoning_since:
                think_s = max(0.0, time.time() - self._reasoning_since)
                tools = ((tools + " · " if tools else "")
                         + f"thinking {think_s:.0f}s")
            body = (f"{elapsed:4.1f}s" + (f" · {tools}" if tools else "")
                    + " · esc to interrupt")
        tail = self._stream_tail[-200:].replace("\n", " ")
        try:
            self._gen.update(Text(f"{frame} {body}", style="cyan")
                             + Text(f"  {tail}", style="dim"))
        except Exception:
            return
        self.refresh_status()

    @work(thread=True, exclusive=True, group="turn")
    def _run_turn(self, message: str) -> None:
        self.cancel.reset()
        self.call_from_thread(self._begin_busy)
        try:
            # A turn must never take the app down. Textual's default is to let
            # a worker exception unwind into WorkerFailed and kill the session
            # (losing the conversation) — that is how one uncaught
            # OSError(ETIMEDOUT) from a repo walk ended a chat on 2026-07-29.
            self.controller.contain("turn", self.controller.run_turn, message,
                                    self.sink, sink=self.sink)
        finally:
            self._call_ui(self._end_busy)

    def _report_backend_error(self, e) -> None:
        """A dead endpoint fails the turn, not the app: record + recovery
        (repair → degrade → /backend hint), rendered into the transcript
        (`ChatController.report_backend_error`)."""
        self.controller.report_backend_error(e, self.sink)

    def _report_turn_crash(self, what: str = "turn") -> None:
        """Render an unexpected worker exception into the transcript instead of
        letting it kill the app; after quit it is only logged
        (`ChatController.report_crash`). Call from inside the `except`."""
        self.controller.report_crash(what, self.sink)

    def _render_outcome(self, outcome, prep, interrupted) -> None:
        """UI thread (`TuiSink.render_outcome` marshals here)."""
        ctl = self.controller
        log = self._log()
        if interrupted:
            ran = outcome.tool_calls
            note = f" ({ran} tool call{'s' if ran != 1 else ''} completed)" if ran else ""
            log.write(f"[yellow]· interrupted — partial turn saved{note}[/]")
            if (kept := attachments_kept_note(self.session)):
                log.write(Text(kept, style="dim"))
            return
        result = outcome.result
        if result is None:
            return
        mode = ("full" if self.session.verbose_level == "full"
                else "compact" if self.session.compact else "truncated")
        log.write(build_final_renderable(outcome.final_text, mode=mode))
        self._session_in += result.prompt_tokens
        self._session_out += result.completion_tokens
        # Spend was already settled by the controller (before this render), so
        # the footer and status bar read the running total.
        footer = (render_footer_text(prep.slot, prep.model, result,
                                     num_ctx=outcome.num_ctx,
                                     ended_at=time.time())
                  + f" · session tok: {self._session_in}+{self._session_out}")
        if self.session.session_cost_usd > 0:
            footer += f" · session cost: {cost_mod.fmt(self.session.session_cost_usd)}"
        log.write(Text(footer, style="dim"))
        # Update the persistent status from the completed turn, then mirror the
        # fill into the live buffer — _end_busy's final _tick would otherwise
        # overwrite it with the stale live estimate.
        ctl.apply_turn_status(outcome)
        self._ctx_pressure = self.status.ctx_pressure
        nxt = ctl.suggest_ctx(outcome)
        if nxt:
            log.write(f"[dim]· context pressure {result.peak_context_pressure:.0%} "
                      f"— `/ctx {nxt[0]}` gives more headroom[/]")
        # A turn the agent loop ABORTED (backend failure contained into the
        # result rather than raised) is a failed turn and must look like one —
        # it used to render as a successful empty reply. Text(), not markup:
        # the reason is an exception string.
        rep = ctl.aborted_report(outcome)
        if rep:
            log.write(Text(f"✗ {rep.reason}", style="red"))
            if rep.ctx_line:
                log.write(Text(f"· {rep.ctx_line}", style="dim"))
            if rep.hint:
                log.write(Text(f"· {rep.hint}", style="yellow"))
            if rep.kept:
                log.write(Text(rep.kept, style="dim"))

    # -- command worker -----------------------------------------------------
    @work(thread=True, exclusive=True, group="turn")
    def _run_command(self, line: str) -> None:
        self.cancel.reset()
        self.call_from_thread(self._begin_busy)
        try:
            # Same contract as the turn worker: a failing command reports and
            # the session survives (a /retry turn or /plan draft that raised
            # included).
            self.controller.contain("command", self._command_body, line,
                                    sink=self.sink)
        finally:
            # The supervisor only returns once the goal is inactive; if
            # something escaped it instead, don't leave it marked active.
            self.session.goal_active = False
            self._call_ui(self._end_busy)

    def _command_body(self, line: str) -> None:
        """One slash command on the command worker (contained by the caller)."""
        res = cmd.dispatch(line, self.ctx)
        if line.strip().lower().startswith("/clear"):
            # The footer's cumulative session tokens describe the cleared
            # conversation too — zero them with the status bar.
            self._session_in = self._session_out = 0
        if getattr(res, "exit", False):
            self.call_from_thread(self.action_quit_app)
            return
        # /retry hands a message back to be run as a turn (same worker, so
        # the busy state and cancel token still cover it).
        if getattr(res, "submit", ""):
            self.controller.run_turn(res.submit, self.sink)
            return
        # /plan and /goal set session flags the line loop would act on; here we
        # run their supervisors on this worker, driving TUI turns + a modal prompt.
        if self.session.plan_pending:
            self.controller.run_plan(self.sink)
        if self.session.goal_active:
            self.controller.run_goal(self.sink)

    # -- prompt_user seam ---------------------------------------------------
    def prompt_user(self, question: str, default: str = "") -> str:
        """Block the calling WORKER for an answer via a modal. Must be called from
        a worker thread, not the UI thread (else it deadlocks)."""
        assert threading.current_thread() is not threading.main_thread(), \
            "prompt_user must be called from a worker thread"
        return self.call_from_thread(self.push_screen_wait, PromptScreen(question, default))

    def run_external(self, argv: list[str]) -> int:
        """Run an interactive terminal program (`/memory edit`'s $EDITOR) with
        the TUI suspended. A bare subprocess would fight the alternate screen
        for the tty. Called from the command worker; the suspend has to happen
        on the UI thread, which blocks there until the program exits."""
        import subprocess

        def _ui() -> int:
            with self.suspend():
                return subprocess.call(argv)

        if threading.current_thread() is threading.main_thread():
            return _ui()
        return self.call_from_thread(_ui)

    def _project_hook(self, target: str | None) -> dict:
        """`/project` / `/index`: run cli's attach (re-resolve + re-index + move
        the repo lock), then re-point the app's own view of the repo so the
        status bar, git segment, and turn setup all follow."""
        if self._on_project is None:
            raise RuntimeError("this session cannot switch projects")
        summary = self._on_project(target)
        # The SESSION is the single live source (controller.apply_project_summary):
        # the status bar, turn setup, compare, and `self.repo_path` all read it
        # at use time.
        apply_project_summary(self.session, summary)
        self.refresh_status()
        return summary

    def _resume_hook(self, session_id: str) -> None:
        """/resume (and --resume on mount): replay a prior transcript into the
        RichLog and extend the live session's turns. Runs on the UI thread at
        mount and on a worker for /resume — LogConsole marshals worker writes
        via call_from_thread."""
        from luxe.chat import resume as resume_mod

        console = LogConsole(self)
        if not session_id:
            resume_mod.list_resumable(console)
            return
        resume_mod.resume_into(session_id, self.session, console)

    # -- feature hooks (run on the worker; reader/console route to the TUI) --
    def _git_hook(self, kind: str, deep: bool | None = None) -> None:
        from luxe.gitkit import run_git_report
        run_git_report(kind, cfg=self.cfg, repo_path=self.session.repo_path,
                       console=LogConsole(self), reader=self._reader, save=True,
                       verbose=(self.session.verbose_level == "full"),
                       expected_head=self.session.index_head, cancel=self.cancel,
                       deep=deep)

    def _compare_hook(self, task: str) -> None:
        try:
            from luxe.compare.run_pair import interactive_compare
        except Exception:
            self.write("[yellow]compare unavailable.[/]")
            return
        interactive_compare(task, self.cfg, self.session.repo_path,
                            self.session.languages,
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
    """Stand-in for `console.status(...)` inside the TUI. Routes the status text to
    the LIVE #generating activity line (app._activity) — NOT a transcript write —
    so gitkit's per-tool `update()` calls don't flood the log (the freeze bug)."""
    def __init__(self, app: ChatApp, message: str = ""):
        self._app = app
        self._message = message

    def __enter__(self):
        if self._message:
            self._app._activity = _plain(self._message)
        return self

    def __exit__(self, *a):
        self._app._activity = None
        return False

    def update(self, text):
        self._app._activity = _plain(text)


def _plain(text) -> str:
    """Strip Rich markup to a plain string for the live activity line."""
    from rich.markup import render as _render
    try:
        return _render(str(text)).plain
    except Exception:
        return str(text)


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
        msg = str(args[0]) if args else ""
        return _NullStatus(self._app, msg)

    def rule(self, *args, **kwargs) -> None:
        self._app.write(Text("─" * 40, style="dim"))

    def __getattr__(self, name):  # graceful no-op for any other console method
        def _noop(*a, **k):
            return None
        return _noop


class TuiSink:
    """The Textual app's `TurnSink` (controller.py).

    Called on the turn WORKER thread. Transcript writes go through
    `ChatApp.write` (thread-safe; looked up per call, never captured) or an
    explicit `_call_ui`; everything the live `#generating` line shows is a
    plain field the UI timer reads — tokens and tool events coalesce there and
    are never marshalled one by one (chat.sdd: flood/deadlock)."""

    crash_hint = ("[yellow]· the session is still alive — retry, or "
                  "`/quit` if it repeats[/]")

    def __init__(self, app: ChatApp) -> None:
        self._app = app

    @property
    def closing(self) -> bool:
        return self._app._exiting

    def print(self, renderable) -> None:
        self._app.write(renderable)

    def choose(self, choices: tuple[str, ...], default: str) -> str:
        raw = (self._app.prompt_user(f"choose [{'/'.join(choices)}]: ")
               or default).strip().lower()
        return raw[:1] if raw[:1] in choices else default

    # -- per-turn lifecycle --------------------------------------------------
    def turn_starting(self) -> None:
        self._app._call_ui(self._app._reset_gen)

    def refused(self, text: str) -> None:
        # HARD spend cap (billable backends only), same rule as the line REPL:
        # refused BEFORE dispatch, never mid-turn, naming the raise command.
        self._app._call_ui(self._app.write, Text(f"✗ {text}", style="red"))

    def on_tool_start(self, command: str) -> None:
        # Worker thread; the UI timer (_tick) reads the plain str — no
        # marshalling needed for a display-only field.
        self._app._running_cmd = command

    def turn_prepared(self, prep: TurnPrep) -> None:
        app = self._app
        # Reflect the window this turn actually uses (incl. a /ctx override).
        app.status.num_ctx = prep.role_cfg.num_ctx
        app.status.ctx_ceiling = prep.ctx_ceiling
        app._call_ui(
            app.write, Text(f"slot: {prep.slot} · model: {prep.model}", style="dim"))

    @contextmanager
    def running(self, prep: TurnPrep, started_at: float):
        app = self._app
        session = app.session

        def _on_tool(tc):
            app._running_cmd = ""  # completed — back to counts-only
            app._tool_counts[getattr(tc, "name", "?")] += 1
            # Per-tool transcript lines only under /verbose (else the live counts
            # on the activity line suffice — avoids the UI-thread write flood).
            if session.verbose_level in ("diff", "full"):
                app._call_ui(
                    app.write, format_tool_call_verbose(tc, session.verbose_level))

        def _on_token(delta):
            app._stream_tail = (app._stream_tail + delta)[-400:]
            app._reasoning_since = 0.0     # answer tokens: it stopped thinking

        def _on_reasoning(delta):
            # Counter only — the reasoning TEXT never reaches the transcript,
            # the fold, or (by default) the screen.
            if not app._reasoning_since:
                app._reasoning_since = time.time()

        def _on_progress(pressure):
            app._ctx_pressure = pressure  # rendered live by the timer

        def _on_notice(text):
            # The loop acting on its own (truncated-turn retry). Goes to the
            # transcript, not the activity line: it must survive the turn, and
            # a retry silently costs minutes of spinner otherwise.
            app._call_ui(app.write, Text(f"· {text}", style="yellow"))

        try:
            yield TurnHooks(on_tool=_on_tool, on_token=_on_token,
                            on_progress=_on_progress, on_notice=_on_notice,
                            on_reasoning=_on_reasoning)
        finally:
            app._reasoning_since = 0.0

    def render_outcome(self, outcome: TurnOutcome, prep: TurnPrep) -> None:
        self._app._call_ui(self._app._render_outcome, outcome, prep,
                           outcome.interrupted)


def run_chat_app(cfg, repo_path, languages, *, keep_loaded=False,
                 resume_session_id=None, dev_mode=False, start_web=False,
                 start_write=False,
                 startup_verbose=None,
                 startup_show_reasoning=False, startup_no_terse=False,
                 startup_debug=False, startup_compact=False, theme_name=None,
                 startup_ctx_tier=None,
                 infer_task_type=None, on_project=None,
                 project_kind="git") -> None:
    """Entry point: build + start the session (`ChatController`, the same
    startup-flag handling as run_chat_repl, so the two front-ends are
    interchangeable), then run the app."""
    # SlotManager notices are routed into the transcript once the app mounts
    # (ChatApp.on_mount); nothing is on screen to print to before that.
    ctl = ChatController.build(
        cfg, repo_path, languages, on_status=lambda m: None,
        keep_loaded=keep_loaded, infer_task_type=infer_task_type,
        theme_name=theme_name, project_kind=project_kind, log=logger,
        dev_mode=dev_mode, start_web=start_web, start_write=start_write,
        startup_verbose=startup_verbose,
        startup_show_reasoning=startup_show_reasoning,
        startup_no_terse=startup_no_terse, startup_debug=startup_debug,
        startup_compact=startup_compact, startup_ctx_tier=startup_ctx_tier,
    )
    # Always-on per-session debug log — the TUI owns the screen, so without
    # it every logger.error traceback was simply lost (chat.sdd).
    ctl.start()

    app = ChatApp(cfg, controller=ctl, resume_session_id=resume_session_id,
                  on_project=on_project)
    try:
        app.run()
    finally:
        # Teardown runs AFTER the app has released the screen, so the notes'
        # one line lands on the real terminal rather than a torn-down
        # alternate screen. No mid-turn special case: `app.run()` returns only
        # after the worker thread has finished (asyncio.run joins the default
        # executor), so no request is in flight here.
        from rich.console import Console as _Console
        ctl.shutdown(_Console(), quiet=True)
