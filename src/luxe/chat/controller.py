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
    rainbow_banner,
    raise_if_cancelled,
)
from luxe.chat.session import CTX_TIERS, ChatSession, aborted_ctx_line, ctx_suggestion
from luxe.chat.status import StatusState
from luxe.chat.turn import (
    TurnOutcome,
    TurnPrep,
    attachments_kept_note,
    finalize_turn,
    is_backend_abort,
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
    model = slots.model_for("chat")
    try:
        org = origin_mod.origin_for(slots.backend, model)
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


def apply_startup_flags(session: ChatSession, *, dev_mode: bool = False,
                        start_web: bool = False, start_write: bool = False,
                        startup_verbose: str | None = None,
                        startup_show_reasoning: bool = False,
                        startup_no_terse: bool = False,
                        startup_debug: bool = False,
                        startup_compact: bool = False,
                        startup_ctx_tier: str | None = None) -> None:
    """The CLI's startup flags, applied to the session before the first turn
    (so /goal users get them without typing REPL commands first)."""
    if dev_mode:
        session.write_enabled = True
        session.unrestricted_bash = True
    if start_web:
        session.web_enabled = True
    if start_write:
        # `luxe code` posture: write tools ON from turn one (its reason to
        # exist is changing code). Bash stays gated — /bash as usual.
        session.write_enabled = True
    # C3: --debug = verbose full + reasoning.
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
              keep_loaded: bool = False,
              infer_task_type: Callable[[str], str] | None = None,
              theme_name: str | None = None,
              project_kind: str = "git",
              log: logging.Logger | None = None,
              **startup_flags) -> "ChatController":
        """Construct the session, its slots and status bar, with the CLI's
        startup flags applied (`apply_startup_flags` names them). Nothing is
        written yet — `start()` opens the transcript."""
        from luxe.agents.tasktype import infer_task_type as _infer_task_type
        from luxe.chat.slots import SlotManager

        infer = infer_task_type or _infer_task_type  # the maintain heuristic
        # C-T: select a curated luxe palette (auto = track terminal/YASL theme).
        if theme_name:
            from luxe.chat import theme as theme_mod
            theme_mod.set_palette(theme_name)
        slots = SlotManager(cfg, on_status=on_status)
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
        apply_startup_flags(session, **startup_flags)
        return cls(cfg, session=session, slots=slots, infer=infer,
                   status=initial_status(slots), keep_loaded=keep_loaded, log=log)

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
        s.steps = result.steps
        s.has_turn = True

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
        (2026-08-24, EVIDENCE.md finding 4). None when not aborted."""
        noted = note_aborted_turn(self.session, self.slots, outcome.result)
        if not noted:
            return None
        reason, hint = noted
        return AbortReport(reason=reason, hint=hint,
                           ctx_line=aborted_ctx_line(outcome.result, outcome.num_ctx),
                           kept=attachments_kept_note(self.session))

    # -- /plan and /goal -----------------------------------------------------
    def run_plan(self, sink: TurnSink, *, run_turn: Callable | None = None) -> None:
        run_plan(self, sink, run_turn=run_turn)

    def run_goal(self, sink: TurnSink, *, run_turn: Callable | None = None) -> None:
        run_goal_loop(self, sink, run_turn=run_turn)


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


def _turn_runner(ctl: ChatController, sink: TurnSink, run_turn: Callable | None):
    """`run_turn(message, plan_mode=False)` for a supervisor: the controller's
    own turn rendered through `sink`, unless a test injects one."""
    if run_turn is not None:
        return run_turn

    def _run(message: str, plan_mode: bool = False) -> TurnOutcome:
        return ctl.run_turn(message, sink, plan_mode=plan_mode)

    return _run


def run_goal_loop(ctl: ChatController, sink: TurnSink, *,
                  run_turn: Callable | None = None) -> None:
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
    session, slots = ctl.session, ctl.slots
    run_turn = _turn_runner(ctl, sink, run_turn)
    done_streak = 0           # consecutive corroborated settled rounds
    settled_no_progress = 0   # consecutive settled rounds with no NEW completed
    completed_ever_grew = False
    thrash_count = 0          # test rounds with no failure improvement (D6)
    best_failures: int | None = None
    best_total: int | None = None
    last_test: tuple[int, int, int] | None = None  # for the honest STUCK message
    recent_fps: list[frozenset] = []
    prev_completed = len(ledger_mod.load(session.session_id).completed)

    sink.print(
        f"[bold cyan]· goal started[/] [dim]{_escape(session.goal)}[/]\n"
        f"[dim]  up to {session.goal_max_rounds} rounds · Ctrl-C/esc halts[/]")
    eff_ctx = session.num_ctx_override or slots.role_for("chat").num_ctx
    if eff_ctx and eff_ctx <= _GOAL_LOW_CTX:
        sink.print(
            f"[yellow]· note: {eff_ctx // 1024}K context is below the practical "
            f"minimum for build tasks — `/ctx large` reduces stuck/incomplete rounds.[/]")

    while session.goal_active:
        if session.goal_round >= session.goal_max_rounds:
            sink.print(f"[yellow]· goal budget reached "
                       f"({session.goal_max_rounds} rounds) — pausing for a human.[/]")
            session.goal_active = False
            break

        session.goal_round += 1
        rnd = session.goal_round
        base = session.goal if rnd == 1 else "continue work"
        message = base + _GOAL_SUFFIX
        # `\[`: an unescaped `[goal round …]` is parsed as a style tag and
        # silently eaten, so the round header never showed.
        sink.print(f"\n[bold]· \\[goal round {rnd}/{session.goal_max_rounds}][/] "
                   f"[dim]{_escape(base)}[/]")

        def _crashed(detail: str) -> bool:
            """Count one failed round; True when the goal must pause."""
            session.consecutive_crashes += 1
            sink.print(Text(
                f"· goal round failed ({detail}) — consecutive "
                f"{session.consecutive_crashes}/{_GOAL_MAX_CRASHES}", style="red"))
            if session.consecutive_crashes >= _GOAL_MAX_CRASHES:
                sink.print("[yellow]· too many consecutive failures — "
                           "pausing goal for a human.[/]")
                session.goal_active = False
                return True
            return False

        try:
            outcome = run_turn(message)
        except (ChatCancelled, KeyboardInterrupt):
            sink.print("[yellow]· goal halted by interrupt.[/]")
            session.goal_active = False
            break
        except BackendError as e:
            # Same record + recovery as an interactive turn (repair → degrade
            # → /backend hint): a failed round is a failed turn, and it used
            # to be counted as a bare "crash" with no error record, no repair
            # and no degrade — so the next round hit the same broken model.
            text, hint = note_backend_error(session, slots, e)
            if hint:
                sink.print(Text(f"· {hint}", style="yellow"))
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
            sink.print("[yellow]· goal paused — the turn was refused "
                       "(see above).[/]")
            session.goal_active = False
            break

        if outcome.interrupted:
            sink.print("[yellow]· goal halted by interrupt.[/]")
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
            sink.print(f"[green]· goal complete ({decision.reason}; completed={ncomp}) "
                       f"after {rnd} round(s).[/]")
            session.goal_active = False
            break

        # Fast trip: identical non-empty tool fingerprint repeating with no edits.
        if outcome.fingerprint:
            recent_fps.append(outcome.fingerprint)
            recent_fps[:] = recent_fps[-_GOAL_STUCK_FP_ROUNDS:]
            if (len(recent_fps) == _GOAL_STUCK_FP_ROUNDS
                    and len(set(recent_fps)) == 1 and settled):
                sink.print(
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
            sink.print(
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


def run_plan(ctl: ChatController, sink: TurnSink, *,
             run_turn: Callable | None = None) -> None:
    """Draft a plan read-only, then ask: save / execute / both / discard (B5).

    The choice goes through `sink.choose` — a `Prompt.ask` on the line REPL,
    a modal in the TUI."""
    from luxe.agents.prompts import PLAN_HINT

    session = ctl.session
    run_turn = _turn_runner(ctl, sink, run_turn)
    objective = (session.plan_pending or "").strip()
    session.plan_pending = None
    if not objective:
        return

    sink.print(f"[bold cyan]· planning[/] [dim]{_escape(objective)}[/]")
    message = f"{objective}\n\n{PLAN_HINT}"
    try:
        outcome = run_turn(message, plan_mode=True)
    except (ChatCancelled, KeyboardInterrupt):
        sink.print("[yellow]· planning interrupted.[/]")
        return

    # A turn refused before dispatch (the spend cap) carries its REFUSAL as
    # `final_text`, and an interrupted or aborted draft carries a fragment;
    # none of them is a plan to save or execute. Same rule as /goal.
    if outcome.crashed:
        sink.print("[yellow]· no plan — the planning turn was refused "
                   "(see above).[/]")
        return
    if outcome.interrupted:
        sink.print("[yellow]· planning interrupted.[/]")
        return
    if getattr(outcome.result, "aborted", False):
        sink.print("[yellow]· no plan — the planning turn aborted "
                   "(see above).[/]")
        return

    plan_text = (outcome.final_text or "").strip()
    if not plan_text:
        sink.print("[yellow]· no plan was produced.[/]")
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
    sink.print(f"\n[bold]Plan ready.[/]  [cyan]s[/]={save_label} · "
               f"[cyan]e[/]xecute · [cyan]b[/]oth · [cyan]d[/]iscard")
    try:
        choice = sink.choose(("s", "e", "b", "d"), "s")
    except (EOFError, KeyboardInterrupt):
        sink.print("[yellow]· plan discarded.[/]")
        return

    if choice in ("s", "b"):
        path = _write_plan_file(session, plan_text)
        if path is None:
            sink.print("[yellow]· plan not saved — ephemeral session with "
                       "no project (`/project <path>` to save into one).[/]")
        else:
            sink.print(f"[green]✓[/] plan written to [cyan]{_escape(str(path))}[/]")

    if choice in ("e", "b"):
        if not session.write_enabled:
            session.write_enabled = True
            sink.print("[yellow]· enabling write mode to execute the plan "
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
        sink.print("[dim]· plan discarded.[/]")
