"""The `luxe chat` interactive loop.

Each user turn drives exactly one `run_single` call (chat.sdd). Conversation
state lives in `ChatSession`; the loop never forks `run_agent`. Streaming/
liveness comes from the existing `on_tool_event` seam; cancellation rides the
same seam via `CancelToken` + `ChatCancelled`.
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from typing import Callable

from rich.console import Console
from rich.live import Live
from rich.markup import escape as _escape
from rich.status import Status

from luxe.agents.single import run_single
from luxe.chat import commands as cmd
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
    CTX_SUGGEST_PRESSURE,
    ChatSession,
    ChatTurn,
    next_tier_up,
)
from luxe.chat.slots import SlotManager
from luxe.chat.status import StatusState
from luxe.config import PipelineConfig
from luxe.memory import project as project_mem
from luxe.memory import session as session_store
from luxe.state import ledger as ledger_mod

@dataclass
class TurnOutcome:
    """What the goal supervisor (B4) needs to decide the next round: how much the
    turn actually did, plus a fingerprint of its tool calls for stuck-loop
    detection. `crashed` is set when run_single raised before returning."""
    tool_calls: int = 0
    files_changed: int = 0
    final_text: str = ""
    interrupted: bool = False
    crashed: bool = False
    fingerprint: frozenset = field(default_factory=frozenset)


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


# Map an inferred task_type onto a model slot. Cosmetic when every slot is the
# champion; meaningful once a slot points elsewhere. `/use` overrides per turn.
_SLOT_FOR_TASK = {
    "implement": "code",
    "bugfix": "code",
    "document": "code",
    "manage": "code",
    "summarize": "plan",
    "review": "chat",
}


def _default_reader(
    console: Console,
    *,
    toolbar_fn: Callable[[], object] | None = None,
    status_markup_fn: Callable[[], str] | None = None,
) -> Callable[[], str]:
    """Return a line reader — prompt_toolkit (with a static bottom-toolbar status
    bar) if available, else input() with the status printed above the prompt."""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.styles import Style

        # Drop prompt_toolkit's default grey/reversed bottom-toolbar bar so the
        # status bar sits on the terminal background (user: "match the terminal").
        toolbar_style = Style.from_dict({
            "bottom-toolbar": "noreverse bg:default fg:default",
            "bottom-toolbar.text": "noreverse bg:default",
        })
        pt = PromptSession(history=InMemoryHistory(), style=toolbar_style)

        def read() -> str:
            # Fresh colors each turn → the arrows shift per render. ptk needs its
            # own ansi* tokens (B4) so the arrows track the terminal palette.
            colors = pick_no_adjacent_repeats(3, palette=ARROW_PALETTE_PTK)
            message = FormattedText(
                [("", "luxe ")]
                + [(f"bold fg:{c}", "›") for c in colors]
                + [("", " ")]
            )
            return pt.prompt(message, bottom_toolbar=toolbar_fn)

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
) -> None:
    from luxe.cli import _infer_task_type  # reuse the maintain heuristic

    infer = infer_task_type or _infer_task_type

    slots = SlotManager(cfg, on_status=lambda m: console.print(f"[dim]· {m}[/]"))
    session = ChatSession(
        repo_path=repo_path,
        project_hash=project_mem.project_hash(repo_path) if repo_path else "",
        languages=languages,
    )
    if dev_mode:
        session.write_enabled = True
        session.unrestricted_bash = True

    # Static bottom-toolbar status bar (chat.sdd lightweight variant): refreshed
    # from `status` between turns; the reader pins it under the input line.
    # Seed num_ctx from the chat slot's role so the bar shows the window size
    # (`ctx 32K`) immediately — before the first turn measures usage.
    status = StatusState(opened_at=time.time(), num_ctx=slots.role_for("chat").num_ctx)
    reader = reader or _default_reader(
        console,
        toolbar_fn=lambda: status_mod.toolbar(session, slots, repo_path, status),
        status_markup_fn=lambda: status_mod.status_markup(session, slots, repo_path, status),
    )
    meta = session_store.new_session(
        repo_path=repo_path,
        project_hash=session.project_hash,
        slot_models=slots.slot_models(),
    )
    session.session_id = meta.session_id

    cancel = CancelToken()
    ctx = cmd.CommandContext(
        console=console,
        session=session,
        slots=slots,
        on_resume=_make_resume_hook(console, session),
        on_compare=_make_compare_hook(console, cfg, repo_path, languages, slots),
        on_compare_review=_make_compare_review_hook(console),
    )

    if resume_session_id:
        ctx.on_resume(resume_session_id)

    # The status bar (under the prompt) already shows repo path, slot/model, and
    # write/bash state — so the banner stays minimal to avoid duplicating it.
    console.print(rainbow_banner("luxe chat")
                  + f"  [dim]session={meta.session_id} · /help for commands[/]")

    try:
        while True:
            # /plan (B5): draft a plan, then maybe execute — runs before the goal
            # check because choosing "execute" sets goal_active for the next pass.
            if session.plan_pending:
                _run_plan(session, slots, cfg, languages, console, cancel,
                          infer, status)
                continue
            # Goal auto-runner (B4): while a goal is active, the supervisor drives
            # rounds itself instead of blocking on the prompt. Returns when the
            # goal completes, pauses, or is interrupted — then we fall back to the
            # normal interactive prompt.
            if session.goal_active:
                _run_goal_loop(session, slots, cfg, languages, console, cancel,
                               infer, status)
                continue
            try:
                line = reader()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            if cmd.is_command(line):
                res = cmd.dispatch(line, ctx)
                if res.exit:
                    break
                continue
            _run_turn(line, session, slots, cfg, languages, console, cancel, infer, status)
    finally:
        if not keep_loaded:
            slots.unload_all()
            console.print("[dim]· models unloaded (use --keep-loaded to keep warm)[/]")
        if slots.stats.count:
            console.print(f"[dim]· session swaps: {slots.stats.count} "
                          f"({slots.stats.seconds:.0f}s total)[/]")


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
    from luxe.mcp.server import make_read_only_role
    from luxe.state import ledger as ledger_mod

    task_type = infer(message)
    slot = session.pinned_slot or _SLOT_FOR_TASK.get(task_type, "chat")
    session.pinned_slot = None

    model = slots.model_for(slot)
    dev_bash = session.write_enabled and session.unrestricted_bash and not plan_mode
    bash_note = " · [red]bash:unrestricted[/]" if dev_bash else ""
    console.print(f"[dim]slot: {slot} · model: {model}{bash_note}[/]")

    backend = slots.backend_for(slot)
    slot_cfg = cfg.slot_config(slot)
    base_role = cfg.role(slot_cfg.role)
    # /plan (B5) forces a read-only drafting turn regardless of write mode.
    write_on = session.write_enabled and not plan_mode
    role_cfg = base_role if write_on else make_read_only_role(base_role)

    # Chat bash (chat.sdd), swapped for THIS run only via run_single's extra-tool
    # seam — benchmark/maintain never pass these, so their bash is untouched.
    # Dev mode → unrestricted; plain write mode → allowlisted bash whose rejections
    # explain the flag state (/bash) instead of leaving the model to retry.
    # update_ledger (B0/B5) is always exposed so the model can maintain its
    # working state across rounds; it only mutates the per-session ledger file.
    from luxe.state.ledger import make_update_ledger_tool
    _led_def, _led_fn = make_update_ledger_tool(session.session_id)
    extra_tool_defs = [_led_def]
    extra_tool_fns = {"update_ledger": _led_fn}
    if write_on:
        from luxe.tools.shell import (
            make_bash_fn,
            restricted_bash_def,
            unrestricted_bash_def,
        )
        if session.unrestricted_bash:
            extra_tool_defs.append(unrestricted_bash_def())
            extra_tool_fns["bash"] = make_bash_fn(unrestricted=True)
        else:
            extra_tool_defs.append(restricted_bash_def())
            extra_tool_fns["bash"] = make_bash_fn(restricted_hint=True)

    # `/ctx` size override (chat-only) — clamp to the role's hard ceiling so a
    # tier request can never exceed what this box/model can hold.
    ctx_ceiling = base_role.num_ctx_max or base_role.num_ctx
    if session.num_ctx_override:
        effective_ctx = min(session.num_ctx_override, ctx_ceiling)
        if effective_ctx != role_cfg.num_ctx:
            role_cfg = role_cfg.model_copy(update={"num_ctx": effective_ctx})

    extra_context, fold_version = session.build_extra_context(message)

    turn_idx = len(session.turns)
    run_id = f"{session.session_id}-{turn_idx}"
    session_store.append_turn(session.session_id, "user", text=message, slot=slot)
    if extra_context:
        session_store.append_fold(session.session_id, turn_idx, fold_version, extra_context)

    cancel.reset()
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

    # Per-turn collectors feeding the ledger (B0/B5) and the goal supervisor (B4):
    # files actually written/edited, and a fingerprint of tool calls (name + the
    # salient arg) for stuck-loop detection.
    changed_files: list[str] = []
    fingerprint: set = set()

    def _note_tool(tc) -> None:
        args = getattr(tc, "arguments", {}) or {}
        prim = (args.get("path") or args.get("query")
                or args.get("command") or args.get("pattern"))
        fingerprint.add((tc.name, str(prim) if prim is not None else ""))
        if (tc.name in ("write_file", "edit_file")
                and not getattr(tc, "error", None)
                and not getattr(tc, "duplicate", False)):
            p = args.get("path")
            if p:
                changed_files.append(str(p))

    def _call(on_event, on_token=None):
        return run_single(
            backend, role_cfg, goal=message, task_type=task_type,
            languages=languages, extra_tool_defs=extra_tool_defs,
            extra_tool_fns=extra_tool_fns, on_tool_event=on_event,
            on_token=on_token, run_id=run_id, phase="chat",
            extra_context=extra_context,
        )

    try:
        if console.is_terminal:
            # Live layout (chat.sdd): tool lines scroll above a status bar that
            # ticks live during the turn (spinner/elapsed/tool count). transient
            # clears the bar when the turn ends; the footer then prints below.
            live_state = StatusState(
                slot=slot, model=model,
                opened_at=(status.opened_at if status else 0.0),
                num_ctx=role_cfg.num_ctx,  # show ctx size during the turn
                ctx_pressure=(status.ctx_pressure if status else 0.0),
                has_turn=(status.has_turn if status else False),  # last-known %
            )
            activity = status_mod.LiveActivity(
                session, slots, session.repo_path, live_state, started_at)
            with Live(activity, console=console, refresh_per_second=10,
                      transient=True) as live:
                reasoner = _ReasoningStreamer(
                    lambda ln: live.console.print(f"[dim]{_escape(ln)}[/]"))

                def _on_event(tc):
                    if session.verbose_level in ("diff", "full"):
                        live.console.print(
                            format_tool_call_verbose(tc, session.verbose_level))
                    else:
                        live.console.print(format_tool_call(tc))
                    activity.note(tc)
                    _note_tool(tc)
                    raise_if_cancelled(cancel)

                def _on_token(delta):
                    # B1: cancel lands mid-generation (cadence-bound, not instant).
                    raise_if_cancelled(cancel)
                    activity.on_token(delta)
                    if session.show_reasoning:
                        reasoner.feed(delta)

                result = _call(_on_event, _on_token)
                if session.show_reasoning:
                    reasoner.flush()
        else:
            reasoner = _ReasoningStreamer(
                lambda ln: console.print(f"[dim]{_escape(ln)}[/]"))
            base_event = make_tool_event(console, cancel, session.verbose_level)

            def _on_event(tc):
                _note_tool(tc)
                base_event(tc)

            def _on_token(delta):
                raise_if_cancelled(cancel)
                if session.show_reasoning:
                    reasoner.feed(delta)

            with Status("[dim]generating…[/]", console=console, spinner="dots"):
                result = _call(_on_event, _on_token)
            if session.show_reasoning:
                reasoner.flush()
    except (ChatCancelled, KeyboardInterrupt):
        interrupted = True
        console.print("[yellow]· interrupted — partial turn saved[/]")
    finally:
        ended_at = time.time()
        if prev_handler is not None:
            try:
                signal.signal(signal.SIGINT, prev_handler)
            except (ValueError, OSError):
                pass

    # Deterministic ledger channel (B0/B5): record files written/edited even on
    # an interrupted turn — those writes already happened on disk.
    if changed_files:
        ledger_mod.record_files(session.session_id, changed_files)

    assistant_text = (result.final_text if result else "") or ""
    if not interrupted and result is not None:
        render_final(console, assistant_text)
        render_footer(
            console,
            slot=slot,
            model=model,
            write_enabled=session.write_enabled,
            result=result,
            swap_count=slots.stats.count,
            swap_seconds=slots.stats.seconds,
            started_at=started_at,
            ended_at=ended_at,
        )
        if status is not None:
            status.slot = slot
            status.model = model
            status.wall_s = result.wall_s
            status.tok_per_s = (result.completion_tokens / result.wall_s
                                if result.wall_s > 0 else 0.0)
            status.ctx_pressure = result.peak_context_pressure
            status.num_ctx = role_cfg.num_ctx
            status.prompt_tokens = result.prompt_tokens
            status.steps = result.steps
            status.has_turn = True
        # Auto-suggest a larger window (never resizes silently — chat.sdd).
        if result.peak_context_pressure >= CTX_SUGGEST_PRESSURE:
            nxt = next_tier_up(role_cfg.num_ctx, ctx_ceiling)
            if nxt:
                console.print(
                    f"[dim]· context pressure {result.peak_context_pressure:.0%} — "
                    f"`/ctx {nxt[0]}` (num_ctx {nxt[1]}) gives more headroom[/]"
                )

        # Working-state view (B2): show the ledger after the footer so the
        # operator sees decided/done/remaining at a glance.
        if session.verbose_level in ("diff", "full"):
            console.print(ledger_mod.render_rich(ledger_mod.load(session.session_id)))

    session_store.append_turn(
        session.session_id, "assistant",
        text=assistant_text, run_id=run_id, interrupted=interrupted,
        steps=(result.steps if result else 0),
        tool_calls=(result.tool_calls_total if result else 0),
    )
    session_store.touch(session.session_id)
    session.add_turn(ChatTurn(
        user=message, assistant=assistant_text, slot=slot, model=model, run_id=run_id,
    ))

    return TurnOutcome(
        tool_calls=(result.tool_calls_total if result else 0),
        files_changed=len(set(changed_files)),
        final_text=assistant_text,
        interrupted=interrupted,
        fingerprint=frozenset(fingerprint),
    )


# -- goal auto-runner (B1/B4) -----------------------------------------------

_GOAL_DONE_ROUNDS = 2       # consecutive corroborated settled rounds → done
_GOAL_STUCK_SETTLED = 3     # settled rounds with no NEW completed items → stuck
_GOAL_STUCK_FP_ROUNDS = 3   # identical tool fingerprint, no edits → faster trip
_GOAL_MAX_CRASHES = 3       # consecutive crashes → pause for a human
_GOAL_LOW_CTX = 32768       # ≤ this is below the practical minimum for builds
_GOAL_SENTINEL = "LUXE_GOAL_DONE"

_GOAL_SUFFIX = (
    "\n\n(Autonomous goal mode. Keep your working state current with the "
    "update_ledger tool: record finished work in `completed` and clear those "
    "items from `in_progress` as you go. When the objective is FULLY complete and "
    f"verified, end your reply with a line containing only {_GOAL_SENTINEL}.)"
)


def _run_goal_loop(
    session: ChatSession,
    slots: SlotManager,
    cfg: PipelineConfig,
    languages: frozenset,
    console: Console,
    cancel: CancelToken,
    infer: Callable[[str], str],
    status: StatusState | None,
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
    done_streak = 0           # consecutive corroborated settled rounds
    settled_no_progress = 0   # consecutive settled rounds with no NEW completed
    recent_fps: list[frozenset] = []
    prev_completed = len(ledger_mod.load(session.session_id).completed)

    console.print(
        f"[bold cyan]· goal started[/] [dim]{_escape(session.goal)}[/]\n"
        f"[dim]  up to {session.goal_max_rounds} rounds · Ctrl-C or /goal stop to halt[/]")
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
        console.print(f"\n[bold]· [goal round {rnd}/{session.goal_max_rounds}][/] "
                      f"[dim]{_escape(base)}[/]")

        try:
            outcome = _run_turn(message, session, slots, cfg, languages,
                                console, cancel, infer, status)
        except (ChatCancelled, KeyboardInterrupt):
            console.print("[yellow]· goal halted by interrupt.[/]")
            session.goal_active = False
            break
        except Exception as e:  # crash: bounded retry on CONSECUTIVE failures
            session.consecutive_crashes += 1
            console.print(f"[red]· goal round crashed ({type(e).__name__}: {e}) — "
                          f"consecutive {session.consecutive_crashes}/"
                          f"{_GOAL_MAX_CRASHES}[/]")
            if session.consecutive_crashes >= _GOAL_MAX_CRASHES:
                console.print("[yellow]· too many consecutive crashes — "
                              "pausing goal for a human.[/]")
                session.goal_active = False
            continue
        session.consecutive_crashes = 0

        if outcome.interrupted:
            console.print("[yellow]· goal halted by interrupt.[/]")
            session.goal_active = False
            break

        # Read the (pruned) ledger to judge progress this round.
        led = ledger_mod.prune(session.session_id)
        ncomp = len(led.completed)
        new_completed = ncomp > prev_completed
        prev_completed = ncomp
        settled = outcome.files_changed == 0
        sentinel = _GOAL_SENTINEL in (outcome.final_text or "")

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

        # Honest STUCK exit: settled rounds that record no NEW completed work.
        # Keys on new completed entries (not any ledger write) so a model
        # rewriting the same in_progress item can't dodge the guard.
        settled_no_progress = settled_no_progress + 1 if (settled and not new_completed) else 0
        if settled_no_progress >= _GOAL_STUCK_SETTLED:
            console.print(
                f"[yellow]· goal STUCK — {_GOAL_STUCK_SETTLED} settled rounds with no "
                f"new completed work (completed={ncomp}). Pausing for a human; "
                f"the objective is likely NOT done.[/]")
            session.goal_active = False
            break

        # Completion (ledger-corroborated): settled + real completed work + no
        # open in_progress, AND the model either signaled done or went tool-idle.
        corroborated = (settled and ncomp > 0 and not led.in_progress
                        and (sentinel or outcome.tool_calls == 0))
        done_streak = done_streak + 1 if corroborated else 0
        if done_streak >= _GOAL_DONE_ROUNDS:
            why = "signaled done" if sentinel else "idle, ledger settled"
            console.print(f"[green]· goal complete ({why}; completed={ncomp}) "
                          f"after {rnd} round(s).[/]")
            session.goal_active = False
            break

    session.goal_round = 0  # ready for a fresh /goal


# -- /plan mode (B5) --------------------------------------------------------


def _write_plan_file(session: ChatSession, plan_text: str):
    """Write the drafted plan, never clobbering an existing plan.md."""
    from pathlib import Path

    base = Path(session.repo_path or ".")
    target = base / "plan.md"
    if target.exists():
        plans_dir = base / ".luxe" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        sid = (session.session_id or "plan")[:8]
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
) -> None:
    """Draft a plan read-only, then ask: save / execute / both / discard (B5)."""
    from pathlib import Path

    from luxe.agents.prompts import PLAN_HINT

    objective = (session.plan_pending or "").strip()
    session.plan_pending = None
    if not objective:
        return

    console.print(f"[bold cyan]· planning[/] [dim]{_escape(objective)}[/]")
    message = f"{objective}\n\n{PLAN_HINT}"
    try:
        outcome = _run_turn(message, session, slots, cfg, languages,
                            console, cancel, infer, status, plan_mode=True)
    except (ChatCancelled, KeyboardInterrupt):
        console.print("[yellow]· planning interrupted.[/]")
        return

    plan_text = (outcome.final_text or "").strip()
    if not plan_text:
        console.print("[yellow]· no plan was produced.[/]")
        return
    session.plan_text = plan_text

    # Interactive choice. plan.md-exists changes only the save destination/label.
    exists = (Path(session.repo_path or ".") / "plan.md").exists()
    save_label = ("save to alternate path (existing plan.md found)" if exists
                  else "save to plan.md")
    console.print(f"\n[bold]Plan ready.[/]  [cyan]s[/]={save_label} · "
                  f"[cyan]e[/]xecute · [cyan]b[/]oth · [cyan]d[/]iscard")
    try:
        from rich.prompt import Prompt
        choice = Prompt.ask("choose", choices=["s", "e", "b", "d"], default="s").lower()
    except (EOFError, KeyboardInterrupt):
        console.print("[yellow]· plan discarded.[/]")
        return

    if choice in ("s", "b"):
        path = _write_plan_file(session, plan_text)
        console.print(f"[green]✓[/] plan written to [cyan]{path}[/]")

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


def _make_compare_hook(console, cfg, repo_path, languages, slots):
    def _compare(task: str) -> None:
        try:
            from luxe.compare.run_pair import interactive_compare
        except Exception:
            console.print("[yellow]compare module unavailable.[/]")
            return
        interactive_compare(task, cfg, repo_path, languages, console=console)

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
