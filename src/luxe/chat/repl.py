"""The `luxe chat` line REPL — the non-TTY / textual-absent front-end.

Each user turn drives exactly one `run_single` call (chat.sdd). Everything
that is not drawing or reading a line — the session, the turn pipeline,
containment, /plan + /goal, teardown — lives in `controller.ChatController`;
this module is the line-oriented `TurnSink` (`LineSink`) plus the read loop.
Streaming/liveness comes from the existing `on_tool_event` seam; cancellation
rides the same seam via `CancelToken` + `ChatCancelled`.
"""

from __future__ import annotations

import logging
import signal
from contextlib import contextmanager
from typing import Callable

from rich.console import Console
from rich.live import Live
from rich.markup import escape as _escape
from rich.status import Status
from rich.text import Text

from luxe.chat import commands as cmd
from luxe.chat.controller import (
    ChatController,
    TurnHooks,
    banner_markup,
    build_hint_markup,
    ephemeral_notice_markup,
)
from luxe.chat.render import (
    ARROW_PALETTE_PTK,
    arrow_prompt_markup,
    format_tool_call,
    format_tool_call_verbose,
    make_tool_event,
    pick_no_adjacent_repeats,
    render_final,
    render_footer,
)
from luxe.chat import status as status_mod
from luxe.chat.session import ChatSession
from luxe.chat.status import StatusState
from luxe.config import PipelineConfig
from luxe.state import ledger as ledger_mod

# Moved to turn.py / controller.py; re-exported so `repl.<name>` keeps working.
from luxe.chat.controller import (  # noqa: F401
    GoalDecision,
    _plan_base,
    _write_plan_file,
    apply_project_summary,
    evaluate_goal_round,
    model_origin_notice,
    start_session_gc,
    startup_ctx_ceiling,
)
from luxe.chat.render import CancelToken, ChatCancelled  # noqa: F401
from luxe.chat.turn import (  # noqa: F401
    _INDEX_TOOLS,
    _SLOT_FOR_TASK,
    TurnOutcome,
    TurnPrep,
    _drop_unavailable_index_tools,
    attachments_kept_note,
    finalize_turn,
    index_tools_available,
    is_backend_abort,
    note_aborted_turn,
    note_backend_error,
    note_turn_crash,
    parse_test_result,
    prepare_turn,
    recover_backend_failure,
    restore_attachments,
    settle_turn_cost,
)

logger = logging.getLogger(__name__)


class _ReasoningStreamer:
    """Buffers streamed model tokens and flushes COMPLETE lines via a callback
    (B2 /reasoning). Line-buffering avoids fighting the rich.Live region — each
    finished line scrolls above it exactly like a tool-call line."""

    def __init__(self, printline: Callable[[str], None]):
        self._buf = ""
        self._printline = printline

    def feed(self, delta: str) -> None:
        self._buf += delta
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._printline(line)

    def flush(self) -> None:
        if self._buf.strip():
            self._printline(self._buf)
        self._buf = ""



def _default_reader(
    console: Console,
    *,
    status_markup_fn: Callable[[], str] | None = None,
) -> Callable[[], str]:
    """Return a line reader. prompt_toolkit if available, else input(). In both
    cases the status line is printed inline just above the prompt so it scrolls
    with the conversation (Claude-CLI style) instead of being pinned to the
    terminal bottom as a floating bar."""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.history import InMemoryHistory

        pt = PromptSession(history=InMemoryHistory())

        def read() -> str:
            # Status line scrolls with history (not a pinned bottom_toolbar).
            if status_markup_fn is not None:
                console.print(status_markup_fn())
            # Fresh colors each turn → the arrows shift per render. ptk needs its
            # own ansi* tokens (B4) so the arrows track the terminal palette.
            colors = pick_no_adjacent_repeats(3, palette=ARROW_PALETTE_PTK)
            message = FormattedText(
                [("", "luxe ")]
                + [(f"bold fg:{c}", "›") for c in colors]
                + [("", " ")]
            )
            return pt.prompt(message)

        return read
    except Exception:  # prompt_toolkit not installed → degrade gracefully
        def read() -> str:
            if status_markup_fn is not None:
                console.print(status_markup_fn())
            console.print(arrow_prompt_markup("luxe"), end="")
            return input()

        return read


def run_chat_repl(
    cfg: PipelineConfig,
    repo_path: str,
    languages: frozenset,
    *,
    console: Console,
    keep_loaded: bool = False,
    resume_session_id: str | None = None,
    reader: Callable[[], str] | None = None,
    infer_task_type: Callable[[str], str] | None = None,
    dev_mode: bool = False,
    start_web: bool = False,
    start_write: bool = False,
    startup_verbose: str | None = None,
    startup_show_reasoning: bool = False,
    startup_no_terse: bool = False,
    startup_debug: bool = False,
    startup_compact: bool = False,
    theme_name: str | None = None,
    startup_ctx_tier: str | None = None,
    on_project: Callable[[str | None], dict] | None = None,
    project_kind: str = "git",
) -> None:
    ctl = ChatController.build(
        cfg, repo_path, languages,
        on_status=lambda m: console.print(Text(f"· {m}", style="dim")),
        keep_loaded=keep_loaded, infer_task_type=infer_task_type,
        theme_name=theme_name, project_kind=project_kind, log=logger,
        dev_mode=dev_mode, start_web=start_web, start_write=start_write,
        startup_verbose=startup_verbose,
        startup_show_reasoning=startup_show_reasoning,
        startup_no_terse=startup_no_terse, startup_debug=startup_debug,
        startup_compact=startup_compact, startup_ctx_tier=startup_ctx_tier,
    )
    session, slots, status = ctl.session, ctl.slots, ctl.status

    # Static status line, printed inline above each prompt (chat.sdd
    # lightweight variant) and refreshed from `status` between turns.
    reader = reader or _default_reader(
        console,
        # session.repo_path, not the startup `repo_path`: `/project` moves it.
        status_markup_fn=lambda: status_mod.status_markup(
            session, slots, session.repo_path, status),
    )
    ctl.start()
    if (_eph_notice := ephemeral_notice_markup()):
        console.print(_eph_notice)

    ctl.ctx = cmd.CommandContext(
        console=console,
        session=session,
        slots=slots,
        on_resume=_make_resume_hook(console, session),
        on_compare=_make_compare_hook(console, cfg, session, slots),
        on_compare_review=_make_compare_review_hook(console),
        on_git_analysis=_make_git_analysis_hook(console, cfg, session, ctl.cancel),
        on_project=_make_project_hook(session, on_project),
        status=status,
        session_log=ctl.dbglog,
    )

    if resume_session_id:
        ctl.ctx.on_resume(resume_session_id)

    # The status line (above the prompt) already shows repo path, slot/model,
    # and write/bash state — so the banner stays minimal to avoid duplicating
    # it. Shared format with the TUI (chat.sdd).
    console.print(banner_markup(session.session_id))
    if (_hint := build_hint_markup()):
        console.print(_hint)
    # Where the weights actually live (local disk / network volume / remote
    # host) — stated once at startup so a networked session is never implicit.
    console.print(model_origin_notice(slots, status))

    sink = LineSink(ctl, console)
    try:
        while True:
            # /plan (B5): draft a plan, then maybe execute — runs before the goal
            # check because choosing "execute" sets goal_active for the next pass.
            if session.plan_pending:
                ctl.cancel.reset()
                ctl.contain("plan", ctl.run_plan, sink, sink=sink)
                continue
            # Goal auto-runner (B4): while a goal is active, the supervisor drives
            # rounds itself instead of blocking on the prompt. Returns when the
            # goal completes, pauses, or is interrupted — then we fall back to the
            # normal interactive prompt.
            if session.goal_active:
                ctl.cancel.reset()
                ctl.contain("goal", ctl.run_goal, sink, sink=sink)
                # The supervisor only returns once the goal is inactive; if
                # something escaped it instead, a goal still marked active
                # would re-enter it forever.
                session.goal_active = False
                continue
            try:
                line = reader()
            except KeyboardInterrupt:
                # ctrl+c CLEARS the line, it does not quit — same contract as
                # the TUI's `action_interrupt` (both readers have already
                # discarded the typed text by the time they raise). ctrl+d
                # (EOFError) is still the exit, as is `/quit`.
                console.print()
                continue
            except EOFError:
                console.print()
                break
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            if cmd.is_command(line):
                res = ctl.dispatch(line, sink)
                if res is None:
                    continue
                if res.exit:
                    break
                if not res.submit:
                    continue
                line = res.submit   # /retry: fall through and run it as a turn
            ctl.contain("turn", ctl.run_turn, line, sink, sink=sink)
    finally:
        ctl.shutdown(console)


class LineSink:
    """The line REPL's `TurnSink` (controller.py): renders a turn into a Rich
    console — a `rich.Live` activity region when the console is a terminal,
    else a `Status` spinner + plain lines (chat.sdd)."""

    crash_hint = ("[yellow]· the session is still alive — retry, "
                  "or /quit if it repeats[/]")
    closing = False

    def __init__(self, ctl: ChatController, console: Console) -> None:
        self.ctl = ctl
        self.console = console

    def print(self, renderable) -> None:
        self.console.print(renderable)

    def choose(self, choices: tuple[str, ...], default: str) -> str:
        from rich.prompt import Prompt
        return Prompt.ask("choose", choices=list(choices), default=default).lower()

    # -- per-turn lifecycle --------------------------------------------------
    def turn_starting(self) -> None:
        pass

    def refused(self, text: str) -> None:
        self.console.print(f"[red]✗ {_escape(text)}[/]")

    def on_tool_start(self, command: str) -> None:
        # Dispatch-time visibility: print the bash command as it STARTS (dim
        # `$` line) so a hung command is identifiable on screen; the usual
        # tool line still summarizes it on completion.
        if command:
            self.console.print(f"[dim]$ {_escape(command)}[/]", highlight=False)

    def turn_prepared(self, prep: TurnPrep) -> None:
        if self.ctl.status is not None:
            self.ctl.status.ctx_ceiling = prep.ctx_ceiling
        bash_note = " · [red]bash:unrestricted[/]" if prep.dev_bash else ""
        self.console.print(
            f"[dim]slot: {prep.slot} · model: {_escape(prep.model)}{bash_note}[/]")
        # The line REPL re-arms the token here, after setup and before the
        # call (the TUI arms it once per worker instead).
        self.ctl.cancel.reset()

    @contextmanager
    def running(self, prep: TurnPrep, started_at: float):
        console, session, cancel, status = (self.console, self.ctl.session,
                                            self.ctl.cancel, self.ctl.status)
        prev_handler = None
        try:
            prev_handler = signal.getsignal(signal.SIGINT)

            def _on_sigint(signum, frame):
                cancel.requested = True

            signal.signal(signal.SIGINT, _on_sigint)
        except (ValueError, OSError):
            prev_handler = None  # not in main thread (e.g. tests)

        try:
            if console.is_terminal:
                # Live layout (chat.sdd): tool lines scroll above a status bar
                # that ticks live during the turn (spinner/elapsed/tool count).
                # transient clears the bar when the turn ends; the footer then
                # prints below.
                live_state = StatusState(
                    slot=prep.slot, model=prep.model,
                    opened_at=(status.opened_at if status else 0.0),
                    num_ctx=prep.role_cfg.num_ctx,  # show ctx size during the turn
                    ctx_ceiling=prep.ctx_ceiling,
                    ctx_pressure=(status.ctx_pressure if status else 0.0),
                    has_turn=(status.has_turn if status else False),  # last-known %
                )
                activity = status_mod.LiveActivity(
                    session, self.ctl.slots, session.repo_path, live_state, started_at)
                with Live(activity, console=console, refresh_per_second=10,
                          transient=True) as live:
                    reasoner = _ReasoningStreamer(
                        lambda ln: live.console.print(f"[dim]{_escape(ln)}[/]"))

                    def _on_tool(tc):
                        if session.verbose_level in ("diff", "full"):
                            live.console.print(
                                format_tool_call_verbose(tc, session.verbose_level))
                        else:
                            # highlight=False: keep markup, stop the
                            # ReprHighlighter repainting the tool name magenta
                            # over the theme (iter-6).
                            live.console.print(format_tool_call(tc), highlight=False)
                        activity.note(tc)

                    def _on_token(delta):
                        activity.on_token(delta)
                        if session.show_reasoning:
                            reasoner.feed(delta)

                    def _on_progress(pressure):
                        # C2: live ctx% during the turn — same instantaneous
                        # metric the [token-progress] line prints, so they agree.
                        live_state.ctx_pressure = pressure
                        live_state.has_turn = True

                    def _on_notice(text):
                        live.console.print(f"[yellow]· {_escape(text)}[/]")

                    # A reasoning model can think for minutes before its first
                    # content token (measured: 10.5 min with nothing on
                    # screen). Feed the counter, never the text.
                    yield TurnHooks(on_tool=_on_tool, on_token=_on_token,
                                    on_progress=_on_progress, on_notice=_on_notice,
                                    on_reasoning=activity.on_reasoning)
                    if session.show_reasoning:
                        reasoner.flush()
            else:
                reasoner = _ReasoningStreamer(
                    lambda ln: console.print(f"[dim]{_escape(ln)}[/]"))
                # Renders the tool line and honours cancel.
                base_event = make_tool_event(console, cancel, session.verbose_level)

                def _on_token(delta):
                    if session.show_reasoning:
                        reasoner.feed(delta)

                def _on_notice(text):
                    console.print(f"[yellow]· {_escape(text)}[/]")

                with Status("[dim]generating…[/]", console=console, spinner="dots"):
                    yield TurnHooks(on_tool=base_event, on_token=_on_token,
                                    on_progress=None, on_notice=_on_notice)
                if session.show_reasoning:
                    reasoner.flush()
        finally:
            if prev_handler is not None:
                try:
                    signal.signal(signal.SIGINT, prev_handler)
                except (ValueError, OSError):
                    pass

    def render_outcome(self, outcome: TurnOutcome, prep: TurnPrep) -> None:
        console, session, slots = self.console, self.ctl.session, self.ctl.slots
        result = outcome.result
        if outcome.interrupted:
            console.print("[yellow]· interrupted — partial turn saved[/]")
            if (kept := attachments_kept_note(session)):
                console.print(Text(kept, style="dim"))
            return
        if result is None:
            return
        # WS4 output ladder: full when /verbose full (or /debug), else compact
        # if /compact, else the default truncated preview.
        out_mode = ("full" if session.verbose_level == "full"
                    else "compact" if session.compact else "truncated")
        render_final(console, outcome.final_text, mode=out_mode)
        render_footer(
            console,
            slot=prep.slot,
            model=prep.model,
            write_enabled=session.write_enabled,
            result=result,
            swap_count=slots.stats.count,
            swap_seconds=slots.stats.seconds,
            started_at=outcome.started_at,
            ended_at=outcome.ended_at,
            num_ctx=prep.role_cfg.num_ctx,
        )
        self.ctl.apply_turn_status(outcome)
        nxt = self.ctl.suggest_ctx(outcome)
        if nxt:
            console.print(
                f"[dim]· context pressure {result.peak_context_pressure:.0%} — "
                f"`/ctx {nxt[0]}` (num_ctx {nxt[1]}) gives more headroom[/]"
            )

        # Working-state view (B2): show the ledger after the footer so the
        # operator sees decided/done/remaining at a glance.
        if session.verbose_level in ("diff", "full"):
            console.print(ledger_mod.render_rich(ledger_mod.load(session.session_id)))

        # LAST, so it is the line left on screen: a turn the loop aborted is a
        # FAILED turn, not an empty answer. Rendered after the footer rather
        # than instead of it — an abort can land after several good steps, and
        # that partial prose is still worth showing.
        rep = self.ctl.aborted_report(outcome)
        if rep:
            console.print(f"[red]✗ {_escape(rep.reason)}[/]")
            if rep.ctx_line:
                console.print(f"[dim]· {rep.ctx_line}[/]")
            if rep.hint:
                console.print(Text(f"· {rep.hint}", style="yellow"))
            if rep.kept:
                console.print(Text(rep.kept, style="dim"))


def _make_project_hook(session, on_project):
    """Wrap cli's attach hook so the SESSION follows the new project too.

    cli owns the lock and the indexes; the session owns repo_path / project_kind
    / index_head, which drive the prompt frame, the git segment, and `/diff`.
    Returns None when the front-end wasn't given a hook (tests, embedders)."""
    if on_project is None:
        return None

    def _hook(target: str | None) -> dict:
        summary = on_project(target)
        apply_project_summary(session, summary)
        return summary

    return _hook


# -- hooks (resume now; compare wired in the compare phase) -----------------


def _make_resume_hook(console: Console, session: ChatSession):
    def _resume(session_id: str) -> None:
        from luxe.chat.resume import list_resumable, resume_into

        if not session_id:
            list_resumable(console)
            return
        resume_into(session_id, session, console)

    return _resume


def _make_compare_hook(console, cfg, session, slots):
    def _compare(task: str) -> None:
        try:
            from luxe.compare.run_pair import interactive_compare
        except Exception:
            console.print("[yellow]compare module unavailable.[/]")
            return
        # Read at call time: `/project` moves both.
        interactive_compare(task, cfg, session.repo_path, session.languages,
                            console=console)

    return _compare


def _make_compare_review_hook(console):
    def _review(compare_id: str) -> None:
        try:
            from luxe.compare.store import review as review_compare
        except Exception:
            console.print("[yellow]compare review unavailable.[/]")
            return
        review_compare(compare_id, console=console)

    return _review


def _make_git_analysis_hook(console, cfg, session: ChatSession, cancel=None):
    """Hook for /gitaudit (and /gitchange) — a read-only gitkit
    report. Targets the SESSION repo, reusing its resident indices (warns if
    HEAD moved); if the session dir isn't a git repo, the runner prompts to
    clone a URL into a local copy and analyzes that, restoring session state."""
    def _git(kind: str, deep: bool | None = None) -> None:
        try:
            from luxe.gitkit import run_git_report
        except Exception:
            console.print("[yellow]gitkit module unavailable.[/]")
            return
        run_git_report(
            kind, cfg=cfg, repo_path=session.repo_path,
            console=console, save=True, expected_head=session.index_head,
            verbose=(session.verbose_level == "full"), cancel=cancel, deep=deep,
        )

    return _git
