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

import logging
import threading
import time
import traceback
from collections import Counter

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, RichLog, Static

from luxe.chat import commands as cmd
from luxe.chat import origin as origin_mod
from luxe.chat import repl as _repl
from luxe.chat import status as status_mod
from luxe.chat.render import (
    ChatCancelled,
    build_final_renderable,
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

    def __init__(self, cfg, repo_path, languages, *, session, slots, infer,
                 keep_loaded=False, resume_session_id=None, on_project=None,
                 dbglog=None):
        super().__init__()
        # `/ephemeral` must detach this before removing the session directory
        # the handler has debug.log open inside.
        self._dbglog = dbglog
        self._resume_id = resume_session_id
        self._on_project = on_project
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
        self._ctx_pressure = 0.0          # live context pressure (on_progress)
        self._activity: str | None = None  # gitkit/compare set this for the live line
        self._running_cmd = ""             # bash command currently executing
        self._timer = None
        self._queue: list[tuple[str, str]] = []  # type-ahead: (display, message) mid-run
        self._paste_chunks: list[tuple[str, str]] = []  # (chip, full text) pending
        self._session_in = 0               # session cumulative prompt tokens
        self._session_out = 0              # session cumulative completion tokens
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
                      max_lines=10000, auto_scroll=True)
        # Bottom block stacks deterministically: generating, status, then input.
        with Vertical(id="bottom"):
            yield Static("", id="generating")
            yield StatusBar(self)
            yield ChatInput(id="prompt", placeholder="message luxe — /help for commands")

    def on_mount(self) -> None:
        from luxe.buildinfo import build_status_hint, version_parts

        log = self._log()
        sha, dirty = version_parts()
        state = "[yellow](dirty)[/]" if dirty else "[dim green](clean)[/]"
        log.write(rainbow_banner("luxe")
                  + f"  [dim]· version {sha}[/] {state} "
                  + f"[dim]· session {self.session.session_id} · /help[/]")
        # The alt-screen TUI is invisible to terminal/tmux scrollback — say so
        # once, with the keys that ARE the scrollback (plus ↑/↓ input history).
        log.write("[dim]· scroll: PgUp/PgDn · shift+↑/↓ · Home/End · mouse "
                  "wheel (terminal scrollback can't see the TUI) · "
                  "↑/↓ recall your prior inputs[/]")
        from luxe import ephemeral
        if (eph_notice := ephemeral.startup_notice()):
            log.write(f"[yellow]·[/] [dim]{eph_notice}[/]")
        hint = build_status_hint()
        if hint:
            log.write(f"[yellow][hint][/] [dim]{hint}[/]")
        # Model provenance (local disk / network volume / remote host), stated
        # once — same notice the line REPL prints.
        log.write(_repl.model_origin_notice(self.slots, self.status))
        # Route SlotManager notices (weight swaps, auto-degrade) into the
        # transcript. run_chat_app constructs slots before the app exists, so
        # the binding lands here; self.write is thread-safe, and the degrade
        # announcement MUST be visible in the TUI, not just the line REPL.
        self.slots._on_status = lambda m: self.write(f"[dim]· {m}[/]")
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
            on_resume=self._resume_hook,
            on_git_analysis=self._git_hook,
            on_compare=self._compare_hook,
            on_compare_review=self._compare_review_hook,
            on_project=self._project_hook,
            status=self.status,
            session_log=self._dbglog,
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
            # Type-ahead: queue it and run after the current task (esc cancels the
            # current one). One run_single per turn is preserved.
            self._queue.append((display, message))
            self.write(f"[yellow]· queued[/] [dim]{display}[/]")
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
        # Input stays ENABLED (type-ahead queue) — submissions during a run are
        # queued, not blocked.
        self._busy = True
        self._gen.display = True
        self._timer = self.set_interval(0.1, self._tick)

    def _reset_gen(self) -> None:
        """Reset the per-turn live buffers (called at the start of each turn so
        goal-loop rounds restart the spinner/preview)."""
        self._stream = ""
        self._tool_counts = Counter()
        self._gen_started = time.time()
        self._ctx_pressure = 0.0
        self._activity = None
        self._running_cmd = ""

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
            body = (f"{elapsed:4.1f}s" + (f" · {tools}" if tools else "")
                    + " · esc to interrupt")
        tail = self._stream[-200:].replace("\n", " ")
        try:
            self._gen.update(Text(f"{frame} {body}", style="cyan")
                             + Text(f"  {tail}", style="dim"))
        except Exception:
            return
        self.refresh_status()

    def _execute_turn_blocking(self, message: str, *, plan_mode: bool = False):
        """Run ONE turn on the current worker thread, rendering into the RichLog.
        Owns no busy/timer state (the outer worker does) so it's reusable by the
        single-turn worker AND the goal/plan loops (which call it per round via
        `_tui_run_turn`)."""
        self.call_from_thread(self._reset_gen)

        def _on_tool_start(command: str) -> None:
            # Worker thread; the UI timer (_tick) reads the plain str — no
            # marshalling needed for a display-only field.
            self._running_cmd = command

        prep = _repl.prepare_turn(message, self.session, self.slots, self.cfg,
                                  self.languages, self.infer, plan_mode=plan_mode,
                                  cancel=self.cancel,
                                  on_tool_start=_on_tool_start)
        # Reflect the window this turn actually uses (incl. a /ctx override).
        self.status.num_ctx = prep.role_cfg.num_ctx
        self.call_from_thread(
            self.write, Text(f"slot: {prep.slot} · model: {prep.model}", style="dim"))

        def _on_event(tc):
            self._running_cmd = ""  # completed — back to counts-only
            self._tool_counts[getattr(tc, "name", "?")] += 1
            prep.note_tool(tc)
            # Per-tool transcript lines only under /verbose (else the live counts
            # on the activity line suffice — avoids the UI-thread write flood).
            if self.session.verbose_level in ("diff", "full"):
                self.call_from_thread(
                    self.write, format_tool_call_verbose(tc, self.session.verbose_level))
            raise_if_cancelled(self.cancel)

        def _on_token(delta):
            raise_if_cancelled(self.cancel)
            self._stream += delta

        def _on_progress(pressure):
            self._ctx_pressure = pressure  # rendered live by the timer

        def _on_notice(text):
            # The loop acting on its own (truncated-turn retry). Goes to the
            # transcript, not the activity line: it must survive the turn, and
            # a retry silently costs minutes of spinner otherwise.
            self.call_from_thread(self.write, Text(f"· {text}", style="yellow"))

        started = time.time()
        interrupted = False
        result = None
        try:
            result = prep.call(_on_event, _on_token, _on_progress, _on_notice)
        except (ChatCancelled, KeyboardInterrupt):
            interrupted = True
        ended = time.time()
        outcome = _repl.finalize_turn(self.session, prep, result,
                                      interrupted=interrupted, message=message,
                                      started_at=started, ended_at=ended,
                                      partial_text=self._stream)
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
        from luxe.backend import BackendError

        self.cancel.reset()
        self.call_from_thread(self._begin_busy)
        try:
            self._execute_turn_blocking(message)
        except BackendError as e:
            # Mirror the line REPL: a dead endpoint fails the turn, not the
            # app; multi-backend configs get the /backend escape hatch.
            self.write(f"[red]✗ {e}[/]")
            logger.error("turn BackendError: %s", e)
            session_store.append_turn(self.session.session_id, "error",
                                      text=str(e),
                                      model=self.slots.backend.model)
            notice = self.slots.note_turn_failure()
            if notice:
                self.write(f"[yellow]· {notice}[/]")
            else:
                hint = self.slots.unreachable_hint()
                if hint:
                    self.write(f"[yellow]· {hint}[/]")
        except Exception:
            # A turn must never take the app down. Textual's default is to let
            # a worker exception unwind into WorkerFailed and kill the session
            # (losing the conversation) — that is how one uncaught
            # OSError(ETIMEDOUT) from a repo walk ended a chat on 2026-07-29.
            self._report_turn_crash()
        finally:
            self.call_from_thread(self._end_busy)

    def _report_turn_crash(self) -> None:
        """Render an unexpected worker exception into the transcript instead of
        letting it kill the app. The full traceback goes to the log so the
        session stays usable and the failure is still diagnosable."""
        exc_line = traceback.format_exc().strip().splitlines()[-1]
        self.write(f"[red]✗ turn failed: {exc_line}[/]")
        self.write("[yellow]· the session is still alive — retry, or "
                   "`/quit` if it repeats[/]")
        logger.error("chat turn crashed\n%s", traceback.format_exc())
        session_store.append_turn(self.session.session_id, "error",
                                  text=exc_line)

    def _render_outcome(self, outcome, prep, interrupted) -> None:
        log = self._log()
        if interrupted:
            ran = outcome.tool_calls
            note = f" ({ran} tool call{'s' if ran != 1 else ''} completed)" if ran else ""
            log.write(f"[yellow]· interrupted — partial turn saved{note}[/]")
            return
        result = outcome.result
        if result is None:
            return
        mode = ("full" if self.session.verbose_level == "full"
                else "compact" if self.session.compact else "truncated")
        log.write(build_final_renderable(outcome.final_text, mode=mode))
        self._session_in += result.prompt_tokens
        self._session_out += result.completion_tokens
        footer = (render_footer_text(prep.slot, prep.model, result,
                                     num_ctx=outcome.num_ctx,
                                     ended_at=time.time())
                  + f" · session tok: {self._session_in}+{self._session_out}")
        log.write(Text(footer, style="dim"))
        # update persistent status from the completed turn
        s = self.status
        s.slot, s.model = prep.slot, prep.model
        s.model_origin = origin_mod.cached_origin_for(
            self.slots.backend, prep.model).kind
        s.wall_s = result.wall_s
        s.tok_per_s = (result.completion_tokens / result.wall_s
                       if result.wall_s > 0 else 0.0)
        # Server-truth context fill, same rule as the line REPL (repl.py): the
        # chars/4 estimate misses tool schemas and reads a flat ~7% at a real
        # ~12%. Also mirror into the live buffer — _end_busy's final _tick
        # would otherwise overwrite this with the stale live estimate.
        if result.last_prompt_tokens and outcome.num_ctx:
            s.ctx_pressure = result.last_prompt_tokens / outcome.num_ctx
        else:
            s.ctx_pressure = result.final_context_pressure
        self._ctx_pressure = s.ctx_pressure
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
                self._execute_turn_blocking(res.submit)
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
        except (ChatCancelled, KeyboardInterrupt):
            self.write("[yellow]· interrupted[/]")
        except Exception:
            # Same contract as the turn worker: a failing command reports and
            # the session survives.
            self._report_turn_crash()
        finally:
            self.call_from_thread(self._end_busy)

    # -- prompt_user seam ---------------------------------------------------
    def prompt_user(self, question: str, default: str = "") -> str:
        """Block the calling WORKER for an answer via a modal. Must be called from
        a worker thread, not the UI thread (else it deadlocks)."""
        assert threading.current_thread() is not threading.main_thread(), \
            "prompt_user must be called from a worker thread"
        return self.call_from_thread(self.push_screen_wait, PromptScreen(question, default))

    def _project_hook(self, target: str | None) -> dict:
        """`/project` / `/index`: run cli's attach (re-resolve + re-index + move
        the repo lock), then re-point the app's own view of the repo so the
        status bar, git segment, and turn setup all follow."""
        if self._on_project is None:
            raise RuntimeError("this session cannot switch projects")
        summary = self._on_project(target)
        self.repo_path = summary["root"]
        self.session.repo_path = summary["root"]
        self.session.project_kind = summary["kind"]
        try:
            from luxe.gitkit.health import current_head
            self.session.index_head = current_head(summary["root"]) or ""
        except Exception:
            self.session.index_head = ""
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


def run_chat_app(cfg, repo_path, languages, *, keep_loaded=False,
                 resume_session_id=None, dev_mode=False, start_web=False,
                 start_write=False,
                 startup_verbose=None,
                 startup_show_reasoning=False, startup_no_terse=False,
                 startup_debug=False, startup_compact=False, theme_name=None,
                 startup_ctx_tier=None,
                 infer_task_type=None, on_project=None,
                 project_kind="git") -> None:
    """Entry point: build the session + app and run it. Mirrors run_chat_repl's
    startup-flag handling so the two front-ends are interchangeable."""
    from luxe.chat.slots import SlotManager
    from luxe.agents.tasktype import infer_task_type as _infer_task_type
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
        project_kind=project_kind,
    )
    # Same one-time stat as run_chat_repl: hint the planeproxy tool when the
    # binary exists on this machine (session.planeproxy_present → PLANEPROXY_HINT).
    from luxe.planeproxy import binary_present as _pp_present
    session.planeproxy_present = _pp_present()
    if dev_mode:
        session.write_enabled = True
        session.unrestricted_bash = True
    if start_web:
        session.web_enabled = True
    if start_write:
        # `luxe code` posture: write tools ON from turn one; bash stays gated.
        session.write_enabled = True
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
    if startup_ctx_tier:
        from luxe.chat.session import CTX_TIERS
        if startup_ctx_tier in CTX_TIERS:
            # `--ctx <tier>` = /ctx before the first turn (clamped per-turn).
            session.num_ctx_override = CTX_TIERS[startup_ctx_tier]

    meta = session_store.new_session(repo_path=repo_path,
                                     project_hash=session.project_hash,
                                     slot_models=slots.slot_models(),
                                     backend_name=slots.backend_name,
                                     base_url=slots.backend.base_url)
    session.session_id = meta.session_id
    session.index_head = current_head(repo_path) if repo_path else ""

    # Always-on per-session debug log — the TUI owns the screen, so without
    # this every logger.error traceback was simply lost (chat.sdd).
    from luxe.chat import debuglog
    dbglog = debuglog.install(session_store.session_dir(meta.session_id))
    logger.info("session %s start · repo=%s · backend=%s (%s) · slots=%s",
                meta.session_id, repo_path or "(none)", slots.backend_name,
                slots.backend.base_url, slots.slot_models())

    app = ChatApp(cfg, repo_path, languages, session=session, slots=slots,
                  infer=infer, keep_loaded=keep_loaded,
                  resume_session_id=resume_session_id, on_project=on_project,
                  dbglog=dbglog)
    try:
        app.run()
    finally:
        # Session working notes — BEFORE the unload (the backend must still be
        # usable) and AFTER the app has released the screen, so its one line
        # lands on the real terminal rather than a torn-down alternate screen.
        # Never raises, never retries (chat/notes.py).
        from luxe.chat import notes as notes_mod
        from rich.console import Console as _Console
        notes_mod.run_session_notes(session, slots, cfg, _Console())
        if not keep_loaded:
            try:
                slots.unload_all()
            except Exception:
                pass
        logger.info("session %s end", session.session_id)
        debuglog.uninstall(dbglog)
