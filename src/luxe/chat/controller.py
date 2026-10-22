"""The UI-agnostic `luxe chat` controller (chat.sdd § "One controller, two
renderers").

`ChatController` owns everything a chat session IS — the `ChatSession`, the
`SlotManager`, the status-bar state, the cancel token, the per-session debug
log, the command context — and everything a session DOES that is not drawing:

- `build()` / `start()` — session construction + startup flags, the
  transcript record, the debug log, session GC;
- `run_turn(message, sink)` — the per-turn pipeline: spend refusal, the
  shared `turn.prepare_turn`, reasoning-sink wiring on the Backend, the one
  `run_single` call, spend settlement, `turn.finalize_turn`;
- `contain(what, fn, …)` / `dispatch(line, sink)` — the containment every
  turn and command runs under (interrupt → message, BackendError → record +
  self-repair → degrade → hint, anything else → record + "still alive");
- `run_plan(sink)` / `run_goal(sink)` — the /plan and /goal supervisors;
- `shutdown(console)` — cancel → session notes → unload (unless
  keep_loaded) → debug log, always in that order.

Front-ends (`repl.py` line REPL, `tui.py` Textual app) implement a small
`TurnSink` and keep only rendering + input. Every shared decision lives here
once, because two copies of it drifted repeatedly (chat.sdd).
"""

from __future__ import annotations

import logging
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from rich.markup import escape as _escape
from rich.text import Text

from luxe import ephemeral
from luxe.backend import BackendError
from luxe.chat import commands as cmd
from luxe.chat import cost as cost_mod
from luxe.chat import origin as origin_mod
from luxe.chat.render import (
    CancelToken,
    ChatCancelled,
    build_final_renderable,
    footer_lines,
    rainbow_banner,
    raise_if_cancelled,
)
from luxe.chat.session import CTX_TIERS, ChatSession, aborted_ctx_line, ctx_suggestion
from luxe.chat.status import StatusState

# The /goal and /plan supervisors live in chat/supervisor.py (2026-10); the
# names callers import from here are re-exported. No cycle: supervisor
# imports this module only under TYPE_CHECKING.
from luxe.chat.supervisor import (  # noqa: F401
    GoalDecision,
    _plan_base,
    _write_plan_file,
    evaluate_goal_round,
    run_goal_loop,
    run_plan,
)
from luxe.chat.turn import (
    TurnOutcome,
    TurnPrep,
    attachments_kept_note,
    finalize_turn,
    note_aborted_turn,
    note_backend_error,
    note_turn_crash,
    prepare_turn,
    settle_turn_cost,
)
from luxe.memory import project as project_mem
from luxe.memory import session as session_store
from luxe.state import ledger as ledger_mod

# Name kept from the module these lines moved out of (debug.log unchanged).
logger = logging.getLogger("luxe.chat.repl")


# -- the front-end seam ------------------------------------------------------


@dataclass
class TurnHooks:
    """The live callbacks a front-end lends ONE `run_single` call (yielded by
    `TurnSink.running`). `on_progress` / `on_reasoning` are optional: None is
    passed through as None, exactly as each front-end always did (the line
    REPL's non-terminal path never had a progress or reasoning sink)."""
    on_tool: Callable[[Any], None]
    on_token: Callable[[str], None]
    on_progress: Callable[[float], None] | None = None
    on_notice: Callable[[str], None] | None = None
    on_reasoning: Callable[[str], None] | None = None


class TurnSink(Protocol):
    """What a front-end implements so the controller can drive it.

    Threading (TUI): the controller calls these from whatever thread runs the
    turn — for the Textual app that is always the worker. A sink therefore
    marshals every UI mutation itself (`call_from_thread`); `print` must be
    callable from any thread."""

    # "· the session is still alive — …" line printed after a crash report.
    crash_hint: str
    # True once the UI is going away: a crash unwinding a worker is then the
    # app closing, not a failed turn — logged, never recorded.
    closing: bool

    def print(self, renderable: Any) -> None: ...
    def choose(self, choices: tuple[str, ...], default: str) -> str: ...
    # -- per-turn lifecycle, in call order --
    def turn_starting(self) -> None: ...
    def refused(self, text: str) -> None: ...
    def on_tool_start(self, command: str) -> None: ...
    def turn_prepared(self, prep: TurnPrep) -> None: ...
    def running(self, prep: TurnPrep,
                started_at: float) -> AbstractContextManager[TurnHooks]: ...
    def render_outcome(self, outcome: TurnOutcome, prep: TurnPrep) -> None: ...


@dataclass
class AbortReport:
    """A turn the agent loop ABORTED (see `turn.note_aborted_turn`), with the
    pieces each front-end renders in its own idiom."""
    reason: str
    hint: str | None
    ctx_line: str
    kept: str


# -- startup helpers (shared by both front-ends) -----------------------------


def apply_project_summary(session, summary: dict) -> None:
    """Make the SESSION follow a `/project` / `/index` attach.

    The session is the single live source for everything project-shaped —
    both front-ends read `session.repo_path` / `session.languages` at turn
    time — so a switch lands here once instead of in per-front-end copies
    that went stale (the startup `languages` kept steering lint/typecheck at
    the OLD project's languages)."""
    root = summary["root"]
    session.repo_path = root
    session.project_kind = summary["kind"]
    if "languages" in summary:
        session.languages = frozenset(summary["languages"] or ())
    try:
        session.project_hash = project_mem.project_hash(root) if root else ""
    except Exception:
        session.project_hash = ""
    try:
        from luxe.gitkit.health import current_head
        session.index_head = current_head(root) or ""
    except Exception:
        session.index_head = ""


# -- the command hooks (one copy; both front-ends) ----------------------------
#
# `console` is where a hook prints (the real console on the line path, the
# TUI's transcript shim there) and `reader(prompt)` answers its sub-prompts —
# gitkit's clone URL, the compare vote: `console.input` on the line path, a
# modal in the TUI. Each front-end used to build its own set and they drifted
# (the line REPL's /gitaudit never got a reader).


def make_resume_hook(console, session: ChatSession):
    def _resume(session_id: str) -> None:
        from luxe.chat.resume import list_resumable, resume_into

        if not session_id:
            list_resumable(console)
            return
        resume_into(session_id, session, console)

    return _resume


def make_compare_hook(console, reader, cfg, session: ChatSession):
    def _compare(task: str) -> None:
        try:
            from luxe.compare.run_pair import interactive_compare
        except Exception:
            console.print("[yellow]compare module unavailable.[/]")
            return
        # Read at call time: `/project` moves both.
        interactive_compare(task, cfg, session.repo_path, session.languages,
                            console=console, reader=reader)

    return _compare


def make_compare_review_hook(console):
    def _review(compare_id: str) -> None:
        try:
            from luxe.compare.store import review as review_compare
        except Exception:
            console.print("[yellow]compare review unavailable.[/]")
            return
        review_compare(compare_id, console=console)

    return _review


def make_git_analysis_hook(console, reader, cfg, session: ChatSession,
                           cancel=None):
    """/gitaudit (and /gitchange) — a read-only gitkit report. Targets the
    SESSION repo, reusing its resident indices (warns if HEAD moved); if the
    session dir isn't a git repo, the runner asks `reader` for a URL to clone
    into a local copy and analyzes that, restoring session state."""
    def _git(kind: str, deep: bool | None = None) -> None:
        try:
            from luxe.gitkit import run_git_report
        except Exception:
            console.print("[yellow]gitkit module unavailable.[/]")
            return
        run_git_report(
            kind, cfg=cfg, repo_path=session.repo_path, console=console,
            reader=reader, save=True, expected_head=session.index_head,
            verbose=(session.verbose_level == "full"), cancel=cancel, deep=deep,
        )

    return _git


def make_project_hook(session: ChatSession, on_project):
    """Wrap cli's attach hook so the SESSION follows the new project too.

    cli owns the lock and the indexes; the session owns repo_path /
    project_kind / index_head, which drive the prompt frame, the git segment,
    and `/diff`. None when the front-end was given no hook (tests,
    embedders) — `/project` then says the session can't switch."""
    if on_project is None:
        return None

    def _hook(target: str | None) -> dict:
        summary = on_project(target)
        apply_project_summary(session, summary)
        return summary

    return _hook


def start_session_gc() -> None:
    """Evict old session directories in the background (memory.sdd names the
    eviction policy; nothing ever called it, so `~/.luxe/sessions/` grew
    without bound). Daemon thread: never blocks startup, never raises, and
    skipped entirely in an ephemeral session — deleting is still writing."""
    if ephemeral.is_ephemeral():
        return

    def _gc() -> None:
        try:
            n = session_store.gc_sessions()
            if n:
                logger.info("session gc: evicted %d old session(s)", n)
        except Exception as e:  # noqa: BLE001 — housekeeping must not surface
            logger.debug("session gc skipped: %s: %s", type(e).__name__, e)

    import threading
    threading.Thread(target=_gc, name="luxe-session-gc", daemon=True).start()


def startup_ctx_ceiling(slots) -> int:
    """The chat slot's `/ctx` ceiling, resolved once at startup for the status
    bar (which must not ask the endpoint from a render). 0 if unknown."""
    try:
        return int(slots.ctx_ceiling("chat") or 0)
    except Exception:
        return 0


def initial_status(slots) -> StatusState:
    """The status bar a session opens with. `num_ctx` is seeded from the
    window the FIRST turn will actually use so the bar shows the right size
    (`ctx 32K` / `ctx 128K`) immediately — before that turn measures usage.
    `default_num_ctx` is the role's window everywhere but a billable
    endpoint, which starts wider (chat/session.py)."""
    return StatusState(opened_at=time.time(), num_ctx=slots.default_num_ctx("chat"),
                       ctx_ceiling=startup_ctx_ceiling(slots))


def model_origin_notice(slots, status=None) -> str:
    """Resolve where the chat slot's model actually lives, record it on the
    status bar, and return the one-line startup notice (Rich markup).

    Called once per front-end at startup — this is the ONLY place that pays for
    the `/v1/models/status` lookup; every later read hits the per-endpoint
    cache. Local weights are announced too, not just remote ones: the point is
    that you can always tell, not that you get warned when it's bad.
    """
    from luxe.chat.doctor import _LIVENESS_TIMEOUT_S

    model = slots.model_for("chat")
    try:
        # Bounded: this runs at TUI mount / REPL startup, and the client's
        # read timeout is sized for GENERATION — a wedged endpoint used to
        # hold startup for 10 min locally, 40 on m5.
        org = origin_mod.origin_for(slots.backend, model,
                                    timeout_s=_LIVENESS_TIMEOUT_S)
    except Exception:
        org = origin_mod.ModelOrigin(kind="unknown", model_id=model)
    if status is not None:
        status.model_origin = org.kind
    colour = "yellow" if org.is_over_the_network else "dim"
    return (f"[dim]· model[/] {model} [dim]—[/] "
            f"[{colour}]{org.glyph} {org.describe()}[/]")


def banner_markup(session_id: str) -> str:
    """The startup banner, one format for both front-ends: app name (no mode —
    that's in the status bar) · version + clean/dirty state · session · /help.
    C3: the build (git short-SHA[+dirty]) makes a run traceable to a commit."""
    from luxe.buildinfo import version_parts

    sha, dirty = version_parts()
    state = "[yellow](dirty)[/]" if dirty else "[dim green](clean)[/]"
    return (rainbow_banner("luxe")
            + f"  [dim]· version {sha}[/] {state} "
            + f"[dim]· session {session_id} · /help[/]")


def build_hint_markup() -> str:
    """Actionable-only build hint (behind→pull, ahead→push, dirty→commit);
    "" when clean & current."""
    from luxe.buildinfo import build_status_hint

    hint = build_status_hint()
    return f"[yellow]\\[hint][/] [dim]{_escape(hint)}[/]" if hint else ""


def ephemeral_notice_markup() -> str:
    """The one-line `--ephemeral` startup notice, or ""."""
    notice = ephemeral.startup_notice()
    return f"[yellow]·[/] [dim]{_escape(notice)}[/]" if notice else ""


@dataclass(frozen=True)
class StartupFlags:
    """The `luxe chat` / `luxe code` startup posture, as ONE value
    `launch._run_interactive` hands to whichever front-end it picks, and
    that front-end hands to `ChatController.build` (2026-10: the same
    sixteen keyword arguments used to be threaded, and written out twice,
    through all four). The defaults are a plain `luxe chat`."""
    keep_loaded: bool = False          # --keep-loaded: skip the exit unload
    resume_session_id: str | None = None
    project_kind: str = "git"          # git | dir | none (luxe.project)
    theme_name: str | None = None      # resolved palette name
    dev_mode: bool = False             # --dev: write + unrestricted bash
    start_web: bool = False            # --web
    start_write: bool = False          # the `luxe code` posture
    verbose: str | None = None         # --verbose off|diff|full
    show_reasoning: bool = False       # --show-reasoning
    no_terse: bool = False             # --no-terse
    debug: bool = False                # --debug = verbose full + reasoning
    compact: bool = False              # --compact
    ctx_tier: str | None = None        # --ctx <tier>


def apply_startup_flags(session: ChatSession, flags: StartupFlags) -> None:
    """The CLI's startup flags, applied to the session before the first turn
    (so /goal users get them without typing REPL commands first)."""
    if flags.dev_mode:
        session.write_enabled = True
        session.unrestricted_bash = True
    if flags.start_web:
        session.web_enabled = True
    if flags.start_write:
        # `luxe code` posture: write tools ON from turn one (its reason to
        # exist is changing code). Bash stays gated — /bash as usual.
        session.write_enabled = True
    # C3: --debug = verbose full + reasoning.
    if flags.debug:
        session.verbose_level = "full"
        session.show_reasoning = True
    elif flags.verbose:
        session.verbose_level = flags.verbose
    if flags.show_reasoning:
        session.show_reasoning = True
    if flags.no_terse:
        session.terse = False
    if flags.compact:
        session.compact = True
    if flags.ctx_tier and flags.ctx_tier in CTX_TIERS:
        # `--ctx <tier>` = /ctx before the first turn; the per-turn clamp to
        # the box/model ceiling applies exactly as it does for /ctx.
        session.num_ctx_override = CTX_TIERS[flags.ctx_tier]


# -- the controller ----------------------------------------------------------


class ChatController:
    """One chat session, independent of how it is drawn (module docstring)."""

    def __init__(self, cfg, *, session: ChatSession, slots, infer: Callable[[str], str],
                 status: StatusState | None = None, cancel: CancelToken | None = None,
                 keep_loaded: bool = False, dbglog=None,
                 log: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.session = session
        self.slots = slots
        self.infer = infer
        self.status = status
        self.cancel = cancel or CancelToken()
        self.keep_loaded = keep_loaded
        self.dbglog = dbglog
        # The front-end's logger: debug.log names which front-end ran the
        # session exactly as it did when each one logged its own start/end.
        self.log = log or logger
        self.ctx: cmd.CommandContext | None = None
        # Streamed prose of the CURRENT turn (chunks, not `+=` on one str —
        # that was O(n²) over a long generation). Persisted as the partial
        # answer if the turn is interrupted; reset when the next turn starts.
        self.stream_parts: list[str] = []

    # -- the live project view (the session is the single source) ----------
    @property
    def repo_path(self) -> str:
        return self.session.repo_path

    @property
    def languages(self) -> frozenset:
        return self.session.languages

    # -- lifecycle -----------------------------------------------------------
    @classmethod
    def build(cls, cfg, repo_path: str, languages: frozenset, *,
              on_status: Callable[[str], None],
              flags: StartupFlags = StartupFlags(),
              infer_task_type: Callable[[str], str] | None = None,
              log: logging.Logger | None = None) -> "ChatController":
        """Construct the session, its slots and status bar, with the CLI's
        startup flags applied (`apply_startup_flags`). Nothing is written
        yet — `start()` opens the transcript."""
        from luxe.agents.tasktype import infer_task_type as _infer_task_type
        from luxe.chat.slots import SlotManager

        infer = infer_task_type or _infer_task_type  # the maintain heuristic
        # C-T: select a curated luxe palette (auto = track terminal/YASL theme).
        if flags.theme_name:
            from luxe.chat import theme as theme_mod
            theme_mod.set_palette(flags.theme_name)
        slots = SlotManager(cfg, on_status=on_status)
        session = ChatSession(
            repo_path=repo_path,
            project_hash=project_mem.project_hash(repo_path) if repo_path else "",
            languages=languages,
            project_kind=flags.project_kind,
        )
        # One cheap stat at session build: machines with the user's SSH-tunnel
        # tool get PLANEPROXY_HINT in the frame (session.py); others see nothing.
        from luxe.planeproxy import binary_present as _pp_present
        session.planeproxy_present = _pp_present()
        apply_startup_flags(session, flags)
        return cls(cfg, session=session, slots=slots, infer=infer,
                   status=initial_status(slots), keep_loaded=flags.keep_loaded,
                   log=log)

    def start(self) -> None:
        """Open the transcript, install the always-on debug log (chat.sdd:
        everything luxe.* logs this session lands in <session dir>/debug.log —
        the post-hoc answer to "what happened" that the screen, especially the
        TUI's, can't provide), start session GC, and record the HEAD the
        resident BM25/symbol indices reflect (so /git* can warn if the repo
        moves mid-session)."""
        from luxe.chat import debuglog

        session, slots = self.session, self.slots
        meta = session_store.new_session(
            repo_path=session.repo_path,
            project_hash=session.project_hash,
            slot_models=slots.slot_models(),
            backend_name=slots.backend_name,
            base_url=slots.backend.base_url,
        )
        session.session_id = meta.session_id
        self.dbglog = debuglog.install(session_store.session_dir(meta.session_id))
        start_session_gc()
        self.log.info("session %s start · repo=%s · backend=%s (%s) · slots=%s",
                      meta.session_id, session.repo_path or "(none)",
                      slots.backend_name, slots.backend.base_url, slots.slot_models())
        if session.repo_path:
            from luxe.gitkit.health import current_head
            session.index_head = current_head(session.repo_path)

    def shutdown(self, console, *, quiet: bool = False) -> None:
        """End the session: cancel anything still polling the token → session
        working notes (BEFORE the unload, while the backend is still usable;
        never raises, never retries, bounded — chat/notes.py) → unload unless
        keep_loaded → detach the debug log. The debug log is detached even if
        an earlier step raised.

        `quiet` is the TUI's exit: the app has already released the screen,
        so the unload is silent and best-effort; the line REPL shows it (it
        can take a few seconds — quitting must not look like a hang)."""
        from luxe.chat import debuglog
        from luxe.chat import notes as notes_mod

        self.cancel.requested = True
        try:
            notes_mod.run_session_notes(self.session, self.slots, self.cfg, console,
                                        timeout_s=notes_mod.EXIT_TIMEOUT_S)
            if not self.keep_loaded:
                if quiet:
                    try:
                        self.slots.unload_all()
                    except Exception:
                        pass
                else:
                    with console.status("[dim]unloading models…[/]", spinner="dots"):
                        self.slots.unload_all()
                    console.print("[dim]· models unloaded (use --keep-loaded to keep warm)[/]")
            if not quiet and self.slots.stats.count:
                console.print(f"[dim]· session swaps: {self.slots.stats.count} "
                              f"({self.slots.stats.seconds:.0f}s total)[/]")
        finally:
            self.log.info("session %s end", self.session.session_id)
            debuglog.uninstall(self.dbglog)

    def command_context(self, console, *, reader: Callable[[str], str],
                        run_external: Callable[[list[str]], int] | None = None,
                        on_project=None) -> cmd.CommandContext:
        """Build (and keep, as `self.ctx`) the CommandContext both front-ends
        dispatch through: the session, slots and status, the debug-log handle
        `/ephemeral` detaches, and the hooks above wired to `console` and
        `reader`. `run_external` runs an interactive program (`/memory edit`)
        — the TUI suspends itself for it; None = a plain subprocess."""
        session = self.session
        self.ctx = cmd.CommandContext(
            console=console,
            session=session,
            slots=self.slots,
            on_resume=make_resume_hook(console, session),
            on_compare=make_compare_hook(console, reader, self.cfg, session),
            on_compare_review=make_compare_review_hook(console),
            on_git_analysis=make_git_analysis_hook(console, reader, self.cfg,
                                                   session, self.cancel),
            on_project=make_project_hook(session, on_project),
            status=self.status,
            session_log=self.dbglog,
            run_external=run_external,
        )
        return self.ctx

    # -- containment ---------------------------------------------------------
    def contain(self, what: str, fn, *args, sink: TurnSink, **kwargs):
        """Run one command/plan/goal/turn so that NOTHING it raises ends the
        session. /pull, /doctor, /net, /repair and /compare used to run bare
        inside the line loop's try/finally, so one exception (or ctrl+c
        mid-/pull) was an EXIT; the TUI's workers need the same, or Textual
        unwinds a worker exception into WorkerFailed and kills the app."""
        try:
            return fn(*args, **kwargs)
        except (ChatCancelled, KeyboardInterrupt):
            sink.print("[yellow]· interrupted[/]")
        except BackendError as e:
            self.report_backend_error(e, sink)
        except Exception:
            self.report_crash(what, sink)
        return None

    def report_backend_error(self, exc, sink: TurnSink) -> None:
        """Interactive turns survive a dead endpoint: record it, run the kit's
        recovery (repair → degrade → /backend hint), keep going. Text(), not
        markup: the message is an exception string."""
        text, hint = note_backend_error(self.session, self.slots, exc)
        sink.print(Text(f"✗ {text}", style="red"))
        if hint:
            sink.print(Text(f"· {hint}", style="yellow"))
        if (kept := attachments_kept_note(self.session)):
            sink.print(Text(kept, style="dim"))

    def report_crash(self, what: str, sink: TurnSink) -> None:
        """One bad turn or command must not end the session (an uncaught
        OSError from a repo walk did exactly that on 2026-07-29): report the
        last line, log the rest. Call from inside the `except`.

        Once the UI is closing, an exception unwinding the worker is the app
        going away (typically `RuntimeError('App is not running')` from a UI
        call), not a failed turn: logged, never recorded as one."""
        if getattr(sink, "closing", False):
            self.log.debug("chat %s ended after quit", what, exc_info=True)
            return
        exc_line = note_turn_crash(self.session, what)
        sink.print(Text(f"✗ {what} failed: {exc_line}", style="red"))
        sink.print(sink.crash_hint)
        if (kept := attachments_kept_note(self.session)):
            sink.print(Text(kept, style="dim"))

    def dispatch(self, line: str, sink: TurnSink):
        """Run one slash command, contained. The cancel token is reset first:
        a token left set by an interrupted turn would cancel the next command
        that polls it (/gitaudit) before it started. Returns the
        CommandResult, or None when the command failed (already reported)."""
        self.cancel.reset()
        return self.contain("command", cmd.dispatch, line, self.ctx, sink=sink)

    # -- the turn ------------------------------------------------------------
    def run_turn(self, message: str, sink: TurnSink, *,
                 plan_mode: bool = False) -> TurnOutcome:
        """ONE interactive turn = exactly one `run_single` call (chat.sdd).

        The spend cap refuses BEFORE dispatch: a refused turn must not persist
        a user record or open a run — nothing about it happened — and is marked
        `crashed` so the autonomous goal supervisor stops instead of spinning.
        Everything after `prepare_turn` settles its spend in a `finally`, so a
        request billed before the turn errored or was interrupted still counts
        against the hard cap. Raises what `run_single` raises other than an
        interrupt (the caller contains it)."""
        self.stream_parts = []
        sink.turn_starting()
        refusal = cost_mod.refusal(self.session, self.slots)
        if refusal:
            sink.refused(refusal)
            return TurnOutcome(crashed=True, final_text=refusal)

        # `cancel` makes the chat bash subprocess killable mid-flight
        # (esc/ctrl+c lands in ~0.2s, not at the timeout); `on_tool_start`
        # shows the command as it STARTS so a hung one is identifiable.
        prep = prepare_turn(message, self.session, self.slots, self.cfg,
                            self.session.languages, self.infer,
                            plan_mode=plan_mode, cancel=self.cancel,
                            on_tool_start=sink.on_tool_start)
        sink.turn_prepared(prep)

        # Reasoning sink for THIS turn, on the Backend instance chat owns (the
        # loop's `backend.chat` call site is frozen — chat.sdd). Cleared in the
        # finally so nothing outside a live turn can be fed into a UI object
        # that no longer exists.
        turn_backend = self.slots.backend
        interrupted = False
        result = None
        started_at = time.time()
        stream_parts = self.stream_parts
        try:
            with sink.running(prep, started_at) as hooks:
                if hooks.on_reasoning is not None:
                    turn_backend.on_reasoning = hooks.on_reasoning

                def _on_event(tc):
                    prep.note_tool(tc)
                    hooks.on_tool(tc)
                    raise_if_cancelled(self.cancel)

                def _on_token(delta):
                    # B1: cancel lands mid-generation (cadence-bound, not instant).
                    raise_if_cancelled(self.cancel)
                    stream_parts.append(delta)
                    hooks.on_token(delta)

                result = prep.call(_on_event, _on_token, hooks.on_progress,
                                   hooks.on_notice)
        except (ChatCancelled, KeyboardInterrupt):
            interrupted = True
        finally:
            ended_at = time.time()
            turn_backend.on_reasoning = None
            # Spend first, and whatever happened.
            settle_turn_cost(self.session, prep, self.status)

        # UI-agnostic bookkeeping (records changed files even on interrupt,
        # persists the assistant turn, builds the outcome the renderer reads).
        outcome = finalize_turn(self.session, prep, result, interrupted=interrupted,
                                message=message, started_at=started_at,
                                ended_at=ended_at,
                                partial_text="".join(stream_parts))
        # Record + recover an aborted turn HERE, on the thread that ran it:
        # recovery can restart oMLX (minutes), and the TUI's renderer runs on
        # the UI thread — it used to freeze the app for the whole repair.
        outcome.abort = self.aborted_report(outcome)
        sink.render_outcome(outcome, prep)
        return outcome

    # -- shared post-turn decisions (the renderers call these) ---------------
    def apply_turn_status(self, outcome: TurnOutcome) -> None:
        """Update the persistent status bar from a completed turn."""
        s = self.status
        result = outcome.result
        if s is None or result is None:
            return
        s.slot, s.model = outcome.slot, outcome.model
        s.model_origin = origin_mod.cached_origin_for(
            self.slots.backend, outcome.model).kind
        s.wall_s = result.wall_s
        s.tok_per_s = (result.completion_tokens / result.wall_s
                       if result.wall_s > 0 else 0.0)
        # Server-truth context fill when the turn reported usage (the chars/4
        # estimate misses tool schemas and read a flat 7% at a real ~12% —
        # 2026-07-30); the estimate is the fallback.
        if result.last_prompt_tokens and outcome.num_ctx:
            s.ctx_pressure = result.last_prompt_tokens / outcome.num_ctx
        else:
            s.ctx_pressure = result.final_context_pressure
        s.num_ctx = outcome.num_ctx
        s.prompt_tokens = result.prompt_tokens
        from luxe.chat.perf import turn_perf
        perf = turn_perf(result)
        s.prefill_tps, s.decode_tps, s.cache_hit = (
            perf.prefill_tps, perf.decode_tps, perf.cache)
        s.steps = result.steps
        s.has_turn = True
        s.session_prompt_tokens += result.prompt_tokens
        s.session_completion_tokens += result.completion_tokens

    def outcome_lines(self, outcome: TurnOutcome, prep: TurnPrep) -> list:
        """Everything a finished turn shows, built ONCE for both renderers,
        which only print the list (2026-10: each had its own copy and they had
        drifted — the TUI dropped swaps, the write mode, the elapsed line and
        the verbose ledger; the REPL the session token totals. Both now show
        the union).

        Applies the turn to the status bar first (`apply_turn_status` — the
        ctx suggestion reads the model origin it records), so call it on the
        thread that owns the status: the TUI's UI thread. It does not record
        or recover anything — `run_turn` already built `outcome.abort`."""
        session = self.session
        if outcome.interrupted:
            ran = outcome.tool_calls
            note = (f" ({ran} tool call{'s' if ran != 1 else ''} completed)"
                    if ran else "")
            lines: list = [Text(f"· interrupted — partial turn saved{note}",
                                style="yellow")]
            if (kept := attachments_kept_note(session)):
                lines.append(Text(kept, style="dim"))
            return lines
        result = outcome.result
        if result is None:
            return []
        # WS4 output ladder: full when /verbose full (or /debug), else compact
        # if /compact, else the default truncated preview.
        mode = ("full" if session.verbose_level == "full"
                else "compact" if session.compact else "truncated")
        lines = [build_final_renderable(outcome.final_text, mode=mode)]
        # Spend was already settled (run_turn's finally), so the footer and
        # status bar read the running total.
        self.apply_turn_status(outcome)
        s = self.status
        lines += footer_lines(
            prep.slot, prep.model, result,
            write_enabled=session.write_enabled, num_ctx=outcome.num_ctx,
            swap_count=self.slots.stats.count,
            swap_seconds=self.slots.stats.seconds,
            started_at=outcome.started_at, ended_at=outcome.ended_at,
            session_tokens=((s.session_prompt_tokens,
                             s.session_completion_tokens)
                            if s is not None else None),
            session_cost_usd=session.session_cost_usd)
        nxt = self.suggest_ctx(outcome)
        if nxt:
            lines.append(Text(
                f"· context pressure {result.peak_context_pressure:.0%} — "
                f"`/ctx {nxt[0]}` (num_ctx {nxt[1]}) gives more headroom",
                style="dim"))
        # Working-state view (B2): decided/done/remaining at a glance.
        if session.verbose_level in ("diff", "full"):
            lines.append(ledger_mod.render_rich(ledger_mod.load(session.session_id)))
        # LAST, so it is the line left on screen: a turn the loop aborted is a
        # FAILED turn, not an empty answer. After the footer rather than
        # instead of it — an abort can land after several good steps, and
        # that partial prose is still worth showing. Text(): the reason is an
        # exception string.
        rep = outcome.abort
        if rep:
            lines.append(Text(f"✗ {rep.reason}", style="red"))
            if rep.ctx_line:
                lines.append(Text(f"· {rep.ctx_line}", style="dim"))
            if rep.hint:
                lines.append(Text(f"· {rep.hint}", style="yellow"))
            if rep.kept:
                lines.append(Text(rep.kept, style="dim"))
        return lines

    def suggest_ctx(self, outcome: TurnOutcome):
        """Auto-suggest a larger window (never resizes silently — chat.sdd),
        and only when a larger window is plausibly the answer: `ctx_suggestion`
        drops the offer when one tool result ate the window, when the turn
        aborted for something more headroom cannot fix, or when the next tier
        needs RAM this host does not have (session.py, 2026-08-24). Display
        gate only — CTX_SUGGEST_PRESSURE and every compaction threshold are
        untouched. Returns `(tier, num_ctx)` or None."""
        s = self.status
        return ctx_suggestion(
            outcome.result, outcome.num_ctx, outcome.ctx_ceiling,
            # `CTX_TIER_MIN_RAM_GB` is arithmetic about the box holding the KV
            # cache; on a remote endpoint that is not this one. Mirrors
            # `cmd_toggles._ctx_on_local_ram`, including "unknown ⇒ local".
            local_weights=(s.model_origin != "remote" if s is not None else True),
        )

    def aborted_report(self, outcome: TurnOutcome) -> AbortReport | None:
        """A turn the loop aborted is a FAILED turn, not an empty answer:
        record it and run the kit's recovery (`turn.note_aborted_turn`), and
        say which step each context number describes — the footer's `ctx:` is
        the last ACCEPTED step, the pressure figure the step that FAILED
        (2026-08-24, EVIDENCE.md finding 4). None when not aborted.

        `run_turn` calls this once per turn, off the UI thread, and carries the
        result on `TurnOutcome.abort`; renderers read that field and must not
        call this (it records, and its recovery can block for minutes)."""
        noted = note_aborted_turn(self.session, self.slots, outcome.result)
        if not noted:
            return None
        reason, hint = noted
        return AbortReport(reason=reason, hint=hint,
                           ctx_line=aborted_ctx_line(outcome.result, outcome.num_ctx),
                           kept=attachments_kept_note(self.session))

    # -- /plan and /goal (chat/supervisor.py) -------------------------------
    def run_plan(self, sink: TurnSink, *, run_turn: Callable | None = None) -> None:
        run_plan(self, sink, run_turn=run_turn)

    def run_goal(self, sink: TurnSink, *, run_turn: Callable | None = None) -> None:
        run_goal_loop(self, sink, run_turn=run_turn)
