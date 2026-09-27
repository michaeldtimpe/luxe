"""The `luxe chat` interactive loop.

Each user turn drives exactly one `run_single` call (chat.sdd). Conversation
state lives in `ChatSession`; the loop never forks `run_agent`. Streaming/
liveness comes from the existing `on_tool_event` seam; cancellation rides the
same seam via `CancelToken` + `ChatCancelled`.
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from typing import Callable

from rich.console import Console
from rich.live import Live
from rich.markup import escape as _escape
from rich.status import Status
from rich.text import Text

from luxe import ephemeral
from luxe.backend import BackendError
from luxe.chat import commands as cmd
from luxe.chat import cost as cost_mod
from luxe.chat.render import (
    ARROW_PALETTE_PTK,
    CancelToken,
    ChatCancelled,
    arrow_prompt_markup,
    format_tool_call,
    format_tool_call_verbose,
    make_tool_event,
    pick_no_adjacent_repeats,
    rainbow_banner,
    raise_if_cancelled,
    render_final,
    render_footer,
)
from luxe.chat import status as status_mod
from luxe.chat.session import (
    CTX_TIERS,
    ChatSession,
    aborted_ctx_line,
    ctx_suggestion,
)
from luxe.chat.slots import SlotManager
from luxe.chat.status import StatusState
from luxe.config import PipelineConfig
from luxe.chat import origin as origin_mod
from luxe.memory import project as project_mem
from luxe.memory import session as session_store
from luxe.state import ledger as ledger_mod

# The UI-agnostic turn core and the session-lifecycle helpers live in
# turn.py / controller.py; re-exported so `repl.<name>` keeps working.
from luxe.chat.controller import (  # noqa: F401
    apply_project_summary,
    model_origin_notice,
    start_session_gc,
    startup_ctx_ceiling,
)
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
    from luxe.agents.tasktype import infer_task_type as _infer_task_type  # the maintain heuristic

    infer = infer_task_type or _infer_task_type

    # C-T: select a curated luxe palette (auto = track terminal/YASL theme).
    if theme_name:
        from luxe.chat import theme as theme_mod
        theme_mod.set_palette(theme_name)

    slots = SlotManager(cfg, on_status=lambda m: console.print(Text(f"· {m}", style="dim")))
    session = ChatSession(
        repo_path=repo_path,
        project_hash=project_mem.project_hash(repo_path) if repo_path else "",
        languages=languages,
        project_kind=project_kind,
    )
    # One cheap stat at session build: machines with the user's SSH-tunnel
    # tool get PLANEPROXY_HINT in the frame (session.py); others see nothing.
    from luxe.planeproxy import binary_present as _pp_present
    session.planeproxy_present = _pp_present()
    if dev_mode:
        session.write_enabled = True
        session.unrestricted_bash = True
    if start_web:
        session.web_enabled = True
    if start_write:
        # `luxe code` posture: write tools ON from turn one (its reason to
        # exist is changing code). Bash stays gated — /bash as usual.
        session.write_enabled = True

    # C3 startup verbosity flags (set before the loop so /goal users get them
    # without typing REPL commands first). --debug = verbose full + reasoning.
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
    if startup_ctx_tier and startup_ctx_tier in CTX_TIERS:
        # `--ctx <tier>` = /ctx before the first turn; the per-turn clamp to
        # the box/model ceiling applies exactly as it does for /ctx.
        session.num_ctx_override = CTX_TIERS[startup_ctx_tier]

    # Static bottom-toolbar status bar (chat.sdd lightweight variant): refreshed
    # from `status` between turns; the reader pins it under the input line.
    # Seed num_ctx from the window the FIRST turn will actually use so the bar
    # shows the right size (`ctx 32K` / `ctx 128K`) immediately — before that
    # turn measures usage. `default_num_ctx` is the role's window everywhere
    # but a billable endpoint, which starts wider (chat/session.py).
    status = StatusState(opened_at=time.time(), num_ctx=slots.default_num_ctx("chat"),
                         ctx_ceiling=startup_ctx_ceiling(slots))
    reader = reader or _default_reader(
        console,
        # session.repo_path, not the startup `repo_path`: `/project` moves it.
        status_markup_fn=lambda: status_mod.status_markup(
            session, slots, session.repo_path, status),
    )
    meta = session_store.new_session(
        repo_path=repo_path,
        project_hash=session.project_hash,
        slot_models=slots.slot_models(),
        backend_name=slots.backend_name,
        base_url=slots.backend.base_url,
    )
    session.session_id = meta.session_id

    # Always-on per-session debug log (chat.sdd): everything luxe.* logs this
    # session lands in <session dir>/debug.log — the post-hoc answer to "what
    # happened" that the screen (especially the TUI) can't provide.
    from luxe.chat import debuglog
    dbglog = debuglog.install(session_store.session_dir(meta.session_id))
    if (_eph_notice := ephemeral.startup_notice()):
        console.print(f"[yellow]·[/] [dim]{_escape(_eph_notice)}[/]")
    start_session_gc()
    logger.info("session %s start · repo=%s · backend=%s (%s) · slots=%s",
                meta.session_id, repo_path or "(none)", slots.backend_name,
                slots.backend.base_url, slots.slot_models())

    # Record the HEAD the resident BM25/symbol indices (built in chat_cmd just
    # before this) reflect, so /git* can warn if the repo moves mid-session.
    if repo_path:
        from luxe.gitkit.health import current_head
        session.index_head = current_head(repo_path)

    cancel = CancelToken()
    ctx = cmd.CommandContext(
        console=console,
        session=session,
        slots=slots,
        on_resume=_make_resume_hook(console, session),
        on_compare=_make_compare_hook(console, cfg, session, slots),
        on_compare_review=_make_compare_review_hook(console),
        on_git_analysis=_make_git_analysis_hook(console, cfg, session, cancel),
        on_project=_make_project_hook(session, on_project),
        status=status,
        session_log=dbglog,
    )

    if resume_session_id:
        ctx.on_resume(resume_session_id)

    # The status bar (under the prompt) already shows repo path, slot/model, and
    # write/bash state — so the banner stays minimal to avoid duplicating it.
    # C3: show the build (git short-SHA[+dirty]) so a run is traceable to a commit.
    from luxe.buildinfo import build_status_hint, version_parts
    # Banner: app name (no mode — that's in the status bar) · version + clean/dirty
    # state · session · /help. Shared format with the TUI (chat.sdd).
    _sha, _dirty = version_parts()
    _state = "[yellow](dirty)[/]" if _dirty else "[dim green](clean)[/]"
    console.print(rainbow_banner("luxe")
                  + f"  [dim]· version {_sha}[/] {_state} "
                  + f"[dim]· session {meta.session_id} · /help[/]")
    # Actionable-only build hint (behind→pull, ahead→push, dirty→commit); silent
    # when clean & current.
    _hint = build_status_hint()
    if _hint:
        console.print(f"[yellow]\\[hint][/] [dim]{_escape(_hint)}[/]")
    # Where the weights actually live (local disk / network volume / remote
    # host) — stated once at startup so a networked session is never implicit.
    console.print(model_origin_notice(slots, status))

    def _contained(what: str, fn, *args, **kwargs):
        """Run one command/plan/goal/turn so that NOTHING it raises ends the
        session — the same containment a turn always had. /pull, /doctor,
        /net, /repair and /compare used to run bare inside the loop's
        try/finally, so one exception (or ctrl+c mid-/pull) was an EXIT."""
        try:
            return fn(*args, **kwargs)
        except (ChatCancelled, KeyboardInterrupt):
            console.print("[yellow]· interrupted[/]")
        except BackendError as e:
            # Interactive turns survive a dead endpoint: report it, run the
            # kit's recovery (repair → degrade → /backend hint), keep going.
            text, hint = note_backend_error(session, slots, e)
            console.print(Text(f"✗ {text}", style="red"))
            if hint:
                console.print(Text(f"· {hint}", style="yellow"))
            if (kept := attachments_kept_note(session)):
                console.print(Text(kept, style="dim"))
        except Exception:
            # One bad turn or command must not end the session (an uncaught
            # OSError from a repo walk did exactly that on 2026-07-29).
            # Report the last line, log the rest.
            exc_line = note_turn_crash(session, what)
            console.print(Text(f"✗ {what} failed: {exc_line}", style="red"))
            console.print("[yellow]· the session is still alive — retry, "
                          "or /quit if it repeats[/]")
            if (kept := attachments_kept_note(session)):
                console.print(Text(kept, style="dim"))
        return None

    try:
        while True:
            # /plan (B5): draft a plan, then maybe execute — runs before the goal
            # check because choosing "execute" sets goal_active for the next pass.
            if session.plan_pending:
                cancel.reset()
                _contained("plan", _run_plan, session, slots, cfg,
                           session.languages, console, cancel, infer, status)
                continue
            # Goal auto-runner (B4): while a goal is active, the supervisor drives
            # rounds itself instead of blocking on the prompt. Returns when the
            # goal completes, pauses, or is interrupted — then we fall back to the
            # normal interactive prompt.
            if session.goal_active:
                cancel.reset()
                _contained("goal", _run_goal_loop, session, slots, cfg,
                           session.languages, console, cancel, infer, status)
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
                # A token left set by an interrupted turn would cancel the
                # next command that polls it (/gitaudit) before it started.
                cancel.reset()
                res = _contained("command", cmd.dispatch, line, ctx)
                if res is None:
                    continue
                if res.exit:
                    break
                if not res.submit:
                    continue
                line = res.submit   # /retry: fall through and run it as a turn
            _contained("turn", _run_turn, line, session, slots, cfg,
                       session.languages, console, cancel, infer, status)
    finally:
        # Session working notes — BEFORE the unload, while the backend is
        # still usable. Never raises, never retries, silent on failure, and
        # bounded (chat/notes.py): quitting must not be held hostage by a
        # nicety.
        from luxe.chat import notes as notes_mod
        notes_mod.run_session_notes(session, slots, cfg, console,
                                    timeout_s=notes_mod.EXIT_TIMEOUT_S)
        if not keep_loaded:
            # WS3: show the unload is happening BEFORE the blocking call (it can
            # take a few seconds) so quitting doesn't look like a hang.
            with console.status("[dim]unloading models…[/]", spinner="dots"):
                slots.unload_all()
            console.print("[dim]· models unloaded (use --keep-loaded to keep warm)[/]")
        if slots.stats.count:
            console.print(f"[dim]· session swaps: {slots.stats.count} "
                          f"({slots.stats.seconds:.0f}s total)[/]")
        logger.info("session %s end", session.session_id)
        debuglog.uninstall(dbglog)



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








def _run_turn(
    message: str,
    session: ChatSession,
    slots: SlotManager,
    cfg: PipelineConfig,
    languages: frozenset,
    console: Console,
    cancel: CancelToken,
    infer: Callable[[str], str],
    status: StatusState | None = None,
    plan_mode: bool = False,
) -> TurnOutcome:
    # Dispatch-time visibility: print the bash command as it STARTS (dim `$`
    # line) so a hung command is identifiable on screen; the usual tool line
    # still summarizes it on completion. `cancel` makes that subprocess
    # killable mid-flight (esc/Ctrl-C lands in ~0.2s, not at the timeout).
    def _on_tool_start(command: str) -> None:
        if command:
            console.print(f"[dim]$ {_escape(command)}[/]", highlight=False)

    # HARD spend cap (billable backends only): refuse BEFORE dispatch. Checked
    # here rather than in `prepare_turn` because a refused turn must not
    # persist a user record or open a run — nothing about it happened. Marked
    # `crashed` so the autonomous goal supervisor stops instead of spinning.
    refusal = cost_mod.refusal(session, slots)
    if refusal:
        console.print(f"[red]✗ {_escape(refusal)}[/]")
        return TurnOutcome(crashed=True, final_text=refusal)

    prep = prepare_turn(message, session, slots, cfg, languages, infer,
                        plan_mode=plan_mode, cancel=cancel,
                        on_tool_start=_on_tool_start)
    role_cfg = prep.role_cfg
    if status is not None:
        status.ctx_ceiling = prep.ctx_ceiling
    bash_note = " · [red]bash:unrestricted[/]" if prep.dev_bash else ""
    console.print(f"[dim]slot: {prep.slot} · model: {_escape(prep.model)}{bash_note}[/]")

    cancel.reset()
    # Reasoning sink for THIS turn, on the Backend instance chat owns (the
    # loop's `backend.chat` call site is frozen — chat.sdd). Cleared in the
    # finally below so nothing outside a live turn can be fed into a UI object
    # that no longer exists.
    turn_backend = slots.backend
    prev_handler = None
    try:
        prev_handler = signal.getsignal(signal.SIGINT)

        def _on_sigint(signum, frame):
            cancel.requested = True

        signal.signal(signal.SIGINT, _on_sigint)
    except (ValueError, OSError):
        prev_handler = None  # not in main thread (e.g. tests)

    interrupted = False
    result = None
    started_at = time.time()
    stream_parts: list[str] = []  # streamed prose; persisted if interrupted

    try:
        if console.is_terminal:
            # Live layout (chat.sdd): tool lines scroll above a status bar that
            # ticks live during the turn (spinner/elapsed/tool count). transient
            # clears the bar when the turn ends; the footer then prints below.
            live_state = StatusState(
                slot=prep.slot, model=prep.model,
                opened_at=(status.opened_at if status else 0.0),
                num_ctx=role_cfg.num_ctx,  # show ctx size during the turn
                ctx_ceiling=prep.ctx_ceiling,
                ctx_pressure=(status.ctx_pressure if status else 0.0),
                has_turn=(status.has_turn if status else False),  # last-known %
            )
            activity = status_mod.LiveActivity(
                session, slots, session.repo_path, live_state, started_at)
            # A reasoning model can think for minutes before its first content
            # token (measured: 10.5 min with nothing on screen). Feed the
            # counter, never the text.
            turn_backend.on_reasoning = activity.on_reasoning
            with Live(activity, console=console, refresh_per_second=10,
                      transient=True) as live:
                reasoner = _ReasoningStreamer(
                    lambda ln: live.console.print(f"[dim]{_escape(ln)}[/]"))

                def _on_event(tc):
                    if session.verbose_level in ("diff", "full"):
                        live.console.print(
                            format_tool_call_verbose(tc, session.verbose_level))
                    else:
                        # highlight=False: keep markup, stop the ReprHighlighter
                        # repainting the tool name magenta over the theme (iter-6).
                        live.console.print(format_tool_call(tc), highlight=False)
                    activity.note(tc)
                    prep.note_tool(tc)
                    raise_if_cancelled(cancel)

                def _on_token(delta):
                    # B1: cancel lands mid-generation (cadence-bound, not instant).
                    raise_if_cancelled(cancel)
                    stream_parts.append(delta)
                    activity.on_token(delta)
                    if session.show_reasoning:
                        reasoner.feed(delta)

                def _on_progress(pressure):
                    # C2: live ctx% during the turn — same instantaneous metric
                    # the [token-progress] line prints, so they agree.
                    live_state.ctx_pressure = pressure
                    live_state.has_turn = True

                def _on_notice(text):
                    live.console.print(f"[yellow]· {_escape(text)}[/]")

                result = prep.call(_on_event, _on_token, _on_progress, _on_notice)
                if session.show_reasoning:
                    reasoner.flush()
        else:
            reasoner = _ReasoningStreamer(
                lambda ln: console.print(f"[dim]{_escape(ln)}[/]"))
            base_event = make_tool_event(console, cancel, session.verbose_level)

            def _on_event(tc):
                prep.note_tool(tc)
                base_event(tc)

            def _on_token(delta):
                raise_if_cancelled(cancel)
                stream_parts.append(delta)
                if session.show_reasoning:
                    reasoner.feed(delta)

            def _on_notice(text):
                console.print(f"[yellow]· {_escape(text)}[/]")

            with Status("[dim]generating…[/]", console=console, spinner="dots"):
                result = prep.call(_on_event, _on_token, None, _on_notice)
            if session.show_reasoning:
                reasoner.flush()
    except (ChatCancelled, KeyboardInterrupt):
        interrupted = True
        console.print("[yellow]· interrupted — partial turn saved[/]")
    finally:
        ended_at = time.time()
        turn_backend.on_reasoning = None
        # Spend first, and whatever happened: a request billed before the
        # turn errored or was interrupted still counts against the hard cap.
        settle_turn_cost(session, prep, status)
        if prev_handler is not None:
            try:
                signal.signal(signal.SIGINT, prev_handler)
            except (ValueError, OSError):
                pass

    # UI-agnostic bookkeeping (records changed files even on interrupt, persists
    # the assistant turn, builds the outcome the renderer reads from).
    outcome = finalize_turn(session, prep, result, interrupted=interrupted,
                            message=message, started_at=started_at,
                            ended_at=ended_at,
                            partial_text="".join(stream_parts))

    if not interrupted and result is not None:
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
            started_at=started_at,
            ended_at=ended_at,
            num_ctx=prep.role_cfg.num_ctx,
        )
        if status is not None:
            status.slot = prep.slot
            status.model = prep.model
            status.model_origin = origin_mod.cached_origin_for(
                slots.backend, prep.model).kind
            status.wall_s = result.wall_s
            status.tok_per_s = (result.completion_tokens / result.wall_s
                                if result.wall_s > 0 else 0.0)
            # Bar shows the server-truth context fill when the turn reported
            # usage (the chars/4 estimate misses tool schemas and read a flat
            # 7% at a real ~12% — 2026-07-30); estimate is the fallback.
            if result.last_prompt_tokens and role_cfg.num_ctx:
                status.ctx_pressure = (result.last_prompt_tokens
                                       / role_cfg.num_ctx)
            else:
                status.ctx_pressure = result.final_context_pressure
            status.num_ctx = role_cfg.num_ctx
            status.prompt_tokens = result.prompt_tokens
            status.steps = result.steps
            status.has_turn = True
        # Auto-suggest a larger window (never resizes silently — chat.sdd),
        # and only when a larger window is plausibly the answer: `ctx_suggestion`
        # drops the offer when one tool result ate the window, when the turn
        # aborted for something more headroom cannot fix, or when the next tier
        # needs RAM this host does not have (session.py, 2026-08-24). Display
        # gate only — CTX_SUGGEST_PRESSURE and every compaction threshold are
        # untouched.
        nxt = ctx_suggestion(
            result, role_cfg.num_ctx, prep.ctx_ceiling,
            # `CTX_TIER_MIN_RAM_GB` is arithmetic about the box holding the KV
            # cache; on a remote endpoint that is not this one. Mirrors
            # `cmd_toggles._ctx_on_local_ram`, including "unknown ⇒ local".
            local_weights=(status.model_origin != "remote"
                           if status is not None else True),
        )
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
        # FAILED turn, not an empty answer (see `note_aborted_turn`). Rendered
        # after the footer rather than instead of it — an abort can land after
        # several good steps, and that partial prose is still worth showing.
        noted = note_aborted_turn(session, slots, result)
        if noted:
            reason, hint = noted
            console.print(f"[red]✗ {_escape(reason)}[/]")
            # Which step each context number describes. Without it the footer's
            # `ctx:` (last ACCEPTED step) reads as a contradiction of the
            # pressure figure (the step that FAILED) — 2026-08-24, EVIDENCE.md
            # finding 4.
            ctx_line = aborted_ctx_line(result, role_cfg.num_ctx)
            if ctx_line:
                console.print(f"[dim]· {ctx_line}[/]")
            if hint:
                console.print(Text(f"· {hint}", style="yellow"))
            if (kept := attachments_kept_note(session)):
                console.print(Text(kept, style="dim"))
    elif interrupted and (kept := attachments_kept_note(session)):
        console.print(Text(kept, style="dim"))

    return outcome


# -- goal auto-runner (B1/B4, C1) -------------------------------------------

_GOAL_DONE_ROUNDS = 2       # consecutive corroborated settled rounds → done
_GOAL_STUCK_SETTLED = 3     # settled rounds with no NEW completed items → stuck (idle)
_GOAL_STUCK_THRASH = 3      # test-running rounds with no failure improvement → stuck
_GOAL_STUCK_FP_ROUNDS = 3   # identical tool fingerprint, no edits → faster trip
_GOAL_MAX_CRASHES = 3       # consecutive crashes → pause for a human
_GOAL_LOW_CTX = 32768       # ≤ this is below the practical minimum for builds
_GOAL_SENTINEL = "LUXE_GOAL_DONE"

_GOAL_SUFFIX = (
    "\n\n(Autonomous goal mode. Cadence: work → tool output → update_ledger → done. "
    "Record finished work in `completed` via update_ledger as you go; do not narrate "
    "or re-summarize. When the objective is FULLY complete and verified (e.g. the "
    f"test suite passes), reply with ONLY a line containing {_GOAL_SENTINEL}.)"
)


@dataclass
class GoalDecision:
    """Result of evaluating one goal round (pure, unit-testable)."""
    verdict: str  # "continue" | "done" | "stuck"
    reason: str   # short machine reason for the verdict
    done_streak: int
    settled_no_progress: int
    completed_ever_grew: bool
    thrash_count: int
    best_failures: int | None
    best_total: int | None


def evaluate_goal_round(
    *, settled: bool, sentinel: bool, completed_count: int, new_completed: bool,
    test_result: tuple[int, int, int] | None,
    done_streak: int, settled_no_progress: int, completed_ever_grew: bool,
    thrash_count: int, best_failures: int | None, best_total: int | None,
    done_rounds: int = _GOAL_DONE_ROUNDS, stuck_settled: int = _GOAL_STUCK_SETTLED,
    stuck_thrash: int = _GOAL_STUCK_THRASH,
) -> GoalDecision:
    """Pure per-round decision for the goal supervisor (C1/D1/D6). Keys on
    OBSERVABLE state, not model bookkeeping:

    DONE when a settled round either (a) shows GREEN tests (observable truth), or
    (b) carries the sentinel + corroborating ledger `completed` (model self-report),
    for 2 consecutive rounds. (a) lets a finished run complete even if the model
    never logged completed / signaled done (the iter-5 run-2/3 false-STUCK).

    STUCK via two independent guards: idle (settled rounds with no new completed) and
    THRASH (rounds that ran tests without reducing failures and added no completed —
    catches the edit→test→same-failures loop the idle counter misses). Completion is
    evaluated first and resets both counters.
    """
    completed_ever_grew = completed_ever_grew or new_completed
    passed = failed = errors = 0
    ran_tests = test_result is not None
    if ran_tests:
        passed, failed, errors = test_result
    failures = failed + errors
    total = passed + failed + errors
    tests_green = ran_tests and failures == 0 and passed > 0

    def _mk(verdict: str, reason: str) -> GoalDecision:
        return GoalDecision(verdict, reason, done_streak, settled_no_progress,
                            completed_ever_grew, thrash_count, best_failures, best_total)

    # --- Completion (observable green OR corroborated sentinel) ---------------
    sentinel_ok = sentinel and completed_count > 0 and completed_ever_grew
    if settled and (tests_green or sentinel_ok):
        done_streak += 1
        settled_no_progress = 0
        thrash_count = 0
        reason = "tests green" if tests_green else "signaled done"
        return _mk("done" if done_streak >= done_rounds else "continue", reason)
    done_streak = 0

    # --- Progress accounting for the thrash guard (D6) ------------------------
    # A round makes progress if failures dropped below the best seen, a broader
    # suite appeared (new baseline), or a new completed item landed.
    new_baseline = ran_tests and (best_total is None or total > best_total)
    improved = ran_tests and (best_failures is None or failures < best_failures or new_baseline)
    if ran_tests:
        if best_total is None or total > best_total:
            best_total = total
        if best_failures is None or failures < best_failures or new_baseline:
            best_failures = failures
    if improved or new_completed:
        thrash_count = 0
    elif ran_tests and failures > 0:
        # Ran tests, no improvement, still failing → thrashing (whether it edited
        # or idled). No-test rounds don't count, so staged work isn't punished.
        thrash_count += 1
    if thrash_count >= stuck_thrash:
        return _mk("stuck", f"thrashing on {failures} failing test(s)")

    # --- Idle STUCK (settled rounds with no new completed work) ---------------
    settled_no_progress = settled_no_progress + 1 if (settled and not new_completed) else 0
    if settled_no_progress >= stuck_settled:
        return _mk("stuck", "no new completed work")
    return _mk("continue", "")


def _run_goal_loop(
    session: ChatSession,
    slots: SlotManager,
    cfg: PipelineConfig,
    languages: frozenset,
    console: Console,
    cancel: CancelToken,
    infer: Callable[[str], str],
    status: StatusState | None,
    run_turn: Callable | None = None,
) -> None:
    """Supervisor: auto-issue rounds until the objective is reached, the budget
    is hit, the agent gets stuck, or too many crashes pile up. Survives a crashed
    round (the turn is already persisted; the ledger + history rehydrate state).

    Completion is LEDGER-AWARE (B1): the iteration-3 data showed `LUXE_GOAL_DONE`
    is self-reported and noisy (a failed 32K run emitted it 20× with an empty
    ledger), while completed-item richness cleanly separated success from failure.
    So a "settled" round (no file edits — re-running tests no longer blocks
    completion) is only treated as DONE when the ledger corroborates (completed
    non-empty AND in_progress cleared), for 2 consecutive rounds. Settled rounds
    that record NO new completed work accrue toward an honest STUCK exit instead.
    """
    run_turn = run_turn or _run_turn  # front-end's turn renderer (line / TUI)
    done_streak = 0           # consecutive corroborated settled rounds
    settled_no_progress = 0   # consecutive settled rounds with no NEW completed
    completed_ever_grew = False
    thrash_count = 0          # test rounds with no failure improvement (D6)
    best_failures: int | None = None
    best_total: int | None = None
    last_test: tuple[int, int, int] | None = None  # for the honest STUCK message
    recent_fps: list[frozenset] = []
    prev_completed = len(ledger_mod.load(session.session_id).completed)

    console.print(
        f"[bold cyan]· goal started[/] [dim]{_escape(session.goal)}[/]\n"
        f"[dim]  up to {session.goal_max_rounds} rounds · Ctrl-C/esc halts[/]")
    eff_ctx = session.num_ctx_override or slots.role_for("chat").num_ctx
    if eff_ctx and eff_ctx <= _GOAL_LOW_CTX:
        console.print(
            f"[yellow]· note: {eff_ctx // 1024}K context is below the practical "
            f"minimum for build tasks — `/ctx large` reduces stuck/incomplete rounds.[/]")

    while session.goal_active:
        if session.goal_round >= session.goal_max_rounds:
            console.print(f"[yellow]· goal budget reached "
                          f"({session.goal_max_rounds} rounds) — pausing for a human.[/]")
            session.goal_active = False
            break

        session.goal_round += 1
        rnd = session.goal_round
        base = session.goal if rnd == 1 else "continue work"
        message = base + _GOAL_SUFFIX
        # `\[`: an unescaped `[goal round …]` is parsed as a style tag and
        # silently eaten, so the round header never showed.
        console.print(f"\n[bold]· \\[goal round {rnd}/{session.goal_max_rounds}][/] "
                      f"[dim]{_escape(base)}[/]")

        def _crashed(detail: str) -> bool:
            """Count one failed round; True when the goal must pause."""
            session.consecutive_crashes += 1
            console.print(Text(
                f"· goal round failed ({detail}) — consecutive "
                f"{session.consecutive_crashes}/{_GOAL_MAX_CRASHES}", style="red"))
            if session.consecutive_crashes >= _GOAL_MAX_CRASHES:
                console.print("[yellow]· too many consecutive failures — "
                              "pausing goal for a human.[/]")
                session.goal_active = False
                return True
            return False

        try:
            outcome = run_turn(message, session, slots, cfg, languages,
                               console, cancel, infer, status)
        except (ChatCancelled, KeyboardInterrupt):
            console.print("[yellow]· goal halted by interrupt.[/]")
            session.goal_active = False
            break
        except BackendError as e:
            # Same record + recovery as an interactive turn (repair → degrade
            # → /backend hint): a failed round is a failed turn, and it used
            # to be counted as a bare "crash" with no error record, no repair
            # and no degrade — so the next round hit the same broken model.
            text, hint = note_backend_error(session, slots, e)
            if hint:
                console.print(Text(f"· {hint}", style="yellow"))
            if _crashed(f"BackendError: {text}"):
                break
            continue
        except Exception as e:  # crash: bounded retry on CONSECUTIVE failures
            note_turn_crash(session, "goal round")
            if _crashed(f"{type(e).__name__}: {e}"):
                break
            continue

        if outcome.crashed:
            # The turn was refused before dispatch (today: the spend cap). The
            # refusal was already printed; retrying cannot change it, and the
            # old loop spent its whole crash budget re-asking.
            console.print("[yellow]· goal paused — the turn was refused "
                          "(see above).[/]")
            session.goal_active = False
            break

        if outcome.interrupted:
            console.print("[yellow]· goal halted by interrupt.[/]")
            session.goal_active = False
            break

        if outcome.result is not None and is_backend_abort(outcome.result):
            # The loop contained the backend failure into an abort; the turn
            # renderer already ran the recovery. Count it like a raised one.
            if _crashed("backend error"):
                break
            continue
        session.consecutive_crashes = 0

        # Read the (pruned) ledger to judge progress this round.
        led = ledger_mod.prune(session.session_id)
        ncomp = len(led.completed)
        new_completed = ncomp > prev_completed
        prev_completed = ncomp
        settled = outcome.files_changed == 0
        sentinel = _GOAL_SENTINEL in (outcome.final_text or "")
        if outcome.test_result is not None:
            last_test = outcome.test_result

        # Observable-signal decision (C1/D6): completion evaluated BEFORE stuck; a
        # corroborated round resets the stuck/thrash counters. Keys on green tests
        # or sentinel+ledger, never plan bookkeeping alone.
        decision = evaluate_goal_round(
            settled=settled, sentinel=sentinel, completed_count=ncomp,
            new_completed=new_completed, test_result=outcome.test_result,
            done_streak=done_streak, settled_no_progress=settled_no_progress,
            completed_ever_grew=completed_ever_grew, thrash_count=thrash_count,
            best_failures=best_failures, best_total=best_total)
        done_streak = decision.done_streak
        settled_no_progress = decision.settled_no_progress
        completed_ever_grew = decision.completed_ever_grew
        thrash_count = decision.thrash_count
        best_failures = decision.best_failures
        best_total = decision.best_total

        if decision.verdict == "done":
            ledger_mod.clear_in_progress(session.session_id)  # cosmetic provenance
            console.print(f"[green]· goal complete ({decision.reason}; completed={ncomp}) "
                          f"after {rnd} round(s).[/]")
            session.goal_active = False
            break

        # Fast trip: identical non-empty tool fingerprint repeating with no edits.
        if outcome.fingerprint:
            recent_fps.append(outcome.fingerprint)
            recent_fps[:] = recent_fps[-_GOAL_STUCK_FP_ROUNDS:]
            if (len(recent_fps) == _GOAL_STUCK_FP_ROUNDS
                    and len(set(recent_fps)) == 1 and settled):
                console.print(
                    f"[yellow]· goal appears stuck — same {len(outcome.fingerprint)} "
                    f"call(s) for {_GOAL_STUCK_FP_ROUNDS} rounds, no edits. "
                    f"Pausing for a human.[/]")
                session.goal_active = False
                break
        else:
            recent_fps.clear()

        if decision.verdict == "stuck":
            # Honest, observable status — distinguishes "nothing happened" from
            # "substantial work, convergence failed" (C1).
            built = len(led.files)
            if last_test is not None:
                p, f, e = last_test
                tests = f"last tests: {p} passed, {f} failed, {e} error(s)"
            else:
                tests = "no test run observed"
            console.print(
                f"[yellow]· goal STUCK ({decision.reason}) after {rnd} round(s) — "
                f"built {built} file(s); {tests}; completed={ncomp}. "
                f"Pausing for a human.[/]")
            session.goal_active = False
            break

    session.goal_round = 0  # ready for a fresh /goal


# -- /plan mode (B5) --------------------------------------------------------


def _plan_base(session: ChatSession):
    """Where `/plan` saves, or None when there is no project to save into.

    A session with no project (`luxe chat` from `~`) used to write `plan.md`
    into the CWD — i.e. drop a file in $HOME. Such a session keeps its plans
    under `~/.luxe/plans/` instead, which is luxe's own state and therefore
    suppressed in an ephemeral session (None)."""
    from pathlib import Path

    if session.repo_path and session.project_kind != "none":
        return Path(session.repo_path)
    if ephemeral.is_ephemeral():
        return None
    from luxe.paths import luxe_home
    return luxe_home() / "plans"


def _write_plan_file(session: ChatSession, plan_text: str):
    """Write the drafted plan, never clobbering an existing plan.md. Returns
    the path, or None when there is nowhere to write it (see `_plan_base`)."""
    base = _plan_base(session)
    if base is None:
        return None
    sid = (session.session_id or "plan")[:8]
    if not session.repo_path or session.project_kind == "none":
        # No project: luxe's own directory, one file per plan.
        base.mkdir(parents=True, exist_ok=True)
        target = base / f"plan-{sid}-{len(session.turns)}.md"
    else:
        target = base / "plan.md"
        if target.exists():
            plans_dir = base / ".luxe" / "plans"
            plans_dir.mkdir(parents=True, exist_ok=True)
            target = plans_dir / f"plan-{sid}-{len(session.turns)}.md"
    target.write_text(plan_text)
    return target


def _run_plan(
    session: ChatSession,
    slots: SlotManager,
    cfg: PipelineConfig,
    languages: frozenset,
    console: Console,
    cancel: CancelToken,
    infer: Callable[[str], str],
    status: StatusState | None,
    run_turn: Callable | None = None,
    reader: Callable[[str], str] | None = None,
) -> None:
    """Draft a plan read-only, then ask: save / execute / both / discard (B5).

    `run_turn`/`reader` are injected by the TUI (renders into the RichLog; asks
    via a modal); both default to the line-REPL behaviour."""
    from luxe.agents.prompts import PLAN_HINT

    run_turn = run_turn or _run_turn
    objective = (session.plan_pending or "").strip()
    session.plan_pending = None
    if not objective:
        return

    console.print(f"[bold cyan]· planning[/] [dim]{_escape(objective)}[/]")
    message = f"{objective}\n\n{PLAN_HINT}"
    try:
        outcome = run_turn(message, session, slots, cfg, languages,
                           console, cancel, infer, status, plan_mode=True)
    except (ChatCancelled, KeyboardInterrupt):
        console.print("[yellow]· planning interrupted.[/]")
        return

    # A turn refused before dispatch (the spend cap) carries its REFUSAL as
    # `final_text`, and an interrupted or aborted draft carries a fragment;
    # none of them is a plan to save or execute. Same rule as /goal.
    if outcome.crashed:
        console.print("[yellow]· no plan — the planning turn was refused "
                      "(see above).[/]")
        return
    if outcome.interrupted:
        console.print("[yellow]· planning interrupted.[/]")
        return
    if getattr(outcome.result, "aborted", False):
        console.print("[yellow]· no plan — the planning turn aborted "
                      "(see above).[/]")
        return

    plan_text = (outcome.final_text or "").strip()
    if not plan_text:
        console.print("[yellow]· no plan was produced.[/]")
        return
    session.plan_text = plan_text

    # Interactive choice. plan.md-exists changes only the save destination/label.
    base = _plan_base(session)
    if base is None:
        save_label = "save (unavailable — ephemeral session, no project)"
    elif not session.repo_path or session.project_kind == "none":
        save_label = f"save under {_escape(str(base))}"
    elif (base / "plan.md").exists():
        save_label = "save to alternate path (existing plan.md found)"
    else:
        save_label = "save to plan.md"
    console.print(f"\n[bold]Plan ready.[/]  [cyan]s[/]={save_label} · "
                  f"[cyan]e[/]xecute · [cyan]b[/]oth · [cyan]d[/]iscard")
    try:
        if reader is not None:
            raw = (reader("choose [s/e/b/d]: ") or "s").strip().lower()
            choice = raw[:1] if raw[:1] in ("s", "e", "b", "d") else "s"
        else:
            from rich.prompt import Prompt
            choice = Prompt.ask("choose", choices=["s", "e", "b", "d"],
                                default="s").lower()
    except (EOFError, KeyboardInterrupt):
        console.print("[yellow]· plan discarded.[/]")
        return

    if choice in ("s", "b"):
        path = _write_plan_file(session, plan_text)
        if path is None:
            console.print("[yellow]· plan not saved — ephemeral session with "
                          "no project (`/project <path>` to save into one).[/]")
        else:
            console.print(f"[green]✓[/] plan written to [cyan]{_escape(str(path))}[/]")

    if choice in ("e", "b"):
        if not session.write_enabled:
            session.write_enabled = True
            console.print("[yellow]· enabling write mode to execute the plan "
                          "(/write to toggle).[/]")
        # Seed the runner: ledger goal + the plan rides in extra_context as
        # provenance so the agent keeps following what it just drafted.
        ledger_mod.apply_update(session.session_id,
                                {"goal": objective, "decided": ["Plan drafted via /plan"]})
        session.goal = objective
        session.goal_round = 0
        session.consecutive_crashes = 0
        session.goal_active = True  # main loop picks this up next iteration
    elif choice == "d":
        console.print("[dim]· plan discarded.[/]")


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
