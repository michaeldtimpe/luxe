"""The `luxe chat` interactive loop.

Each user turn drives exactly one `run_single` call (chat.sdd). Conversation
state lives in `ChatSession`; the loop never forks `run_agent`. Streaming/
liveness comes from the existing `on_tool_event` seam; cancellation rides the
same seam via `CancelToken` + `ChatCancelled`.
"""

from __future__ import annotations

import signal
from typing import Callable

from rich.console import Console
from rich.status import Status

from luxe.agents.single import run_single
from luxe.chat import commands as cmd
from luxe.chat.render import (
    CancelToken,
    ChatCancelled,
    make_tool_event,
    render_final,
    render_footer,
)
from luxe.chat.session import ChatSession, ChatTurn
from luxe.chat.slots import SlotManager
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


def _default_reader(console: Console) -> Callable[[], str]:
    """Return a line reader — prompt_toolkit if available, else input()."""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import InMemoryHistory

        pt = PromptSession(history=InMemoryHistory())

        def read() -> str:
            return pt.prompt("luxe › ")

        return read
    except Exception:  # prompt_toolkit not installed → degrade gracefully
        def read() -> str:
            return input("luxe › ")

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
) -> None:
    from luxe.cli import _infer_task_type  # reuse the maintain heuristic

    infer = infer_task_type or _infer_task_type
    reader = reader or _default_reader(console)

    slots = SlotManager(cfg, on_status=lambda m: console.print(f"[dim]· {m}[/]"))
    session = ChatSession(
        repo_path=repo_path,
        project_hash=project_mem.project_hash(repo_path) if repo_path else "",
        languages=languages,
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

    console.print(f"[bold cyan]luxe chat[/]  [dim]session={meta.session_id}[/]")
    console.print(f"[dim]repo: {repo_path or '(none)'} · "
                  f"slots: {slots.slot_models()} · read-only (/, /help for commands)[/]")

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
            _run_turn(line, session, slots, cfg, languages, console, cancel, infer)
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
) -> None:
    from luxe.mcp.server import make_read_only_role

    task_type = infer(message)
    slot = session.pinned_slot or _SLOT_FOR_TASK.get(task_type, "chat")
    session.pinned_slot = None

    model = slots.model_for(slot)
    console.print(f"[dim]slot: {slot} · model: {model}[/]")

    backend = slots.backend_for(slot)
    slot_cfg = cfg.slot_config(slot)
    base_role = cfg.role(slot_cfg.role)
    role_cfg = base_role if session.write_enabled else make_read_only_role(base_role)

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
    try:
        with Status("[dim]generating…[/]", console=console, spinner="dots"):
            result = run_single(
                backend,
                role_cfg,
                goal=message,
                task_type=task_type,
                languages=languages,
                on_tool_event=on_event,
                run_id=run_id,
                phase="chat",
                extra_context=extra_context,
            )
    except (ChatCancelled, KeyboardInterrupt):
        interrupted = True
        console.print("[yellow]· interrupted — partial turn saved[/]")
    finally:
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
