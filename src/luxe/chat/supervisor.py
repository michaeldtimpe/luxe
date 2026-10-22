"""The `/goal` and `/plan` supervisors for `luxe chat` (chat.sdd).

Moved out of `controller.py` (2026-10, pure move): the autonomous goal
runner — `evaluate_goal_round` is its pure, unit-tested per-round decision —
and the read-only plan draft. Both drive turns through
`ChatController.run_turn` and render through the front-end's `TurnSink`;
`ChatController.run_goal` / `run_plan` are the entry points, and
`controller` / `repl` re-export the names callers already used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from rich.markup import escape as _escape
from rich.text import Text

from luxe import ephemeral
from luxe.backend import BackendError
from luxe.chat.render import ChatCancelled
from luxe.chat.session import ChatSession
from luxe.chat.turn import (
    TurnOutcome,
    is_backend_abort,
    note_backend_error,
    note_turn_crash,
)
from luxe.state import ledger as ledger_mod

if TYPE_CHECKING:
    from luxe.chat.controller import ChatController, TurnSink


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
            # The loop contained the backend failure into an abort;
            # `run_turn` already ran the recovery (`TurnOutcome.abort`).
            # Count it like a raised one.
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
