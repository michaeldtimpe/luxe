"""The `luxe chat` interactive loop.

Each user turn drives exactly one `run_single` call (chat.sdd). Conversation
state lives in `ChatSession`; the loop never forks `run_agent`. Streaming/
liveness comes from the existing `on_tool_event` seam; cancellation rides the
same seam via `CancelToken` + `ChatCancelled`.
"""

from __future__ import annotations

import signal
import time
from typing import Callable

from rich.console import Console
from rich.status import Status

from luxe.agents.single import run_single
from luxe.chat import commands as cmd
from luxe.chat.render import (
    CancelToken,
    ChatCancelled,
    arrow_prompt_markup,
    make_tool_event,
    pick_no_adjacent_repeats,
    rainbow_banner,
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

        pt = PromptSession(history=InMemoryHistory())

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
    status = StatusState(opened_at=time.time())
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

    mode_line = ("[yellow]write+bash (dev mode)[/]" if dev_mode
                 else "read-only")
    console.print(rainbow_banner("luxe chat") + f"  [dim]session={meta.session_id}[/]")
    console.print(f"[dim]repo: {repo_path or '(none)'} · "
                  f"slots: {slots.slot_models()} · {mode_line} (/, /help for commands)[/]")

    try:
        while True:
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
) -> None:
    from luxe.mcp.server import make_read_only_role

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

    # Chat dev mode (chat.sdd): swap the allowlisted bash for an unrestricted one
    # for THIS run only. Chat-scoped via run_single's extra-tool seam — the
    # benchmark/maintain path never passes these, so its bash is untouched.
    extra_tool_defs = None
    extra_tool_fns = None
    if dev_bash:
        from luxe.tools.shell import make_bash_fn, unrestricted_bash_def
        extra_tool_defs = [unrestricted_bash_def()]
        extra_tool_fns = {"bash": make_bash_fn(unrestricted=True)}

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

    on_event = make_tool_event(console, cancel)
    interrupted = False
    result = None
    started_at = time.time()
    try:
        with Status("[dim]generating…[/]", console=console, spinner="dots"):
            result = run_single(
                backend,
                role_cfg,
                goal=message,
                task_type=task_type,
                languages=languages,
                extra_tool_defs=extra_tool_defs,
                extra_tool_fns=extra_tool_fns,
                on_tool_event=on_event,
                run_id=run_id,
                phase="chat",
                extra_context=extra_context,
            )
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
