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
            # Fresh colors each turn → the arrows shift per render.
            colors = pick_no_adjacent_repeats(3)
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
) -> TurnOutcome:
    from luxe.mcp.server import make_read_only_role
    from luxe.state import ledger as ledger_mod

    task_type = infer(message)
    slot = session.pinned_slot or _SLOT_FOR_TASK.get(task_type, "chat")
    session.pinned_slot = None

    model = slots.model_for(slot)
    dev_bash = session.write_enabled and session.unrestricted_bash
    bash_note = " · [red]bash:unrestricted[/]" if dev_bash else ""
    console.print(f"[dim]slot: {slot} · model: {model}{bash_note}[/]")

    backend = slots.backend_for(slot)
    slot_cfg = cfg.slot_config(slot)
    base_role = cfg.role(slot_cfg.role)
    role_cfg = base_role if session.write_enabled else make_read_only_role(base_role)

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
    if session.write_enabled:
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


# -- goal auto-runner (B4) --------------------------------------------------

_GOAL_NOOP_ROUNDS = 2      # consecutive no-op rounds → objective reached
_GOAL_STUCK_ROUNDS = 3     # identical fingerprint, no edits → pathological loop
_GOAL_MAX_CRASHES = 3      # consecutive crashes → pause for a human
_GOAL_SENTINEL = "LUXE_GOAL_DONE"

_GOAL_SUFFIX = (
    "\n\n(Autonomous goal mode. Maintain your working state with the "
    "update_ledger tool as you decide things and finish work. When the objective "
    "is FULLY complete and verified, end your reply with a line containing only "
    f"{_GOAL_SENTINEL}.)"
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
    round (the turn is already persisted; the ledger + history rehydrate state)."""
    noop_streak = 0
    recent_fps: list[frozenset] = []
    console.print(
        f"[bold cyan]· goal started[/] [dim]{_escape(session.goal)}[/]\n"
        f"[dim]  up to {session.goal_max_rounds} rounds · Ctrl-C or /goal stop to halt[/]")

    while session.goal_active:
        if session.goal_round >= session.goal_max_rounds:
            console.print(f"[yellow]· goal budget reached "
                          f"({session.goal_max_rounds} rounds) — pausing.[/]")
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

        # Stuck-loop guard: same non-empty fingerprint repeating with no edits.
        if outcome.fingerprint:
            recent_fps.append(outcome.fingerprint)
            recent_fps[:] = recent_fps[-_GOAL_STUCK_ROUNDS:]
            if (len(recent_fps) == _GOAL_STUCK_ROUNDS
                    and len(set(recent_fps)) == 1
                    and outcome.files_changed == 0):
                console.print(
                    f"[yellow]· goal appears stuck — same {len(outcome.fingerprint)} "
                    f"call(s) for {_GOAL_STUCK_ROUNDS} rounds with no edits. Pausing.[/]")
                session.goal_active = False
                break
        else:
            recent_fps.clear()

        # Completion: no-op rounds are authoritative; the sentinel is advisory and
        # only honored when it coincides with a no-op round.
        no_op = outcome.tool_calls == 0 and outcome.files_changed == 0
        noop_streak = noop_streak + 1 if no_op else 0
        sentinel = _GOAL_SENTINEL in (outcome.final_text or "")
        if noop_streak >= _GOAL_NOOP_ROUNDS or (sentinel and no_op):
            why = "model signaled done" if (sentinel and no_op) else \
                  f"{noop_streak} consecutive no-op rounds"
            console.print(f"[green]· goal complete ({why}) after {rnd} round(s).[/]")
            session.goal_active = False
            break

    session.goal_round = 0  # ready for a fresh /goal


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
