"""Session resume — replay a prior transcript and seed continuation context.

Resume is context-summarized, NOT a KV-cache restore: `run_agent` never re-sees
the raw prior `messages` (chat.sdd). On resume we (1) replay the transcript to
the console for the human, and (2) load the prior user/assistant turns into the
live `ChatSession` so the next turn's `<conversation_history>` fold carries them
forward. The fidelity ceiling is the summarizer, which is surfaced to the user.
"""

from __future__ import annotations

import time

from rich.console import Console

from luxe.chat.session import ChatSession, ChatTurn
from luxe.memory import session as session_store


def list_resumable(console: Console, *, limit: int = 20) -> None:
    metas = session_store.list_sessions()
    if not metas:
        console.print("[dim]No prior chat sessions.[/]")
        return
    console.print(f"[bold]Recent chat sessions[/] ({len(metas)} total)")
    for m in metas[:limit]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.last_active))
        title = m.title or m.repo_path or "(no repo)"
        console.print(f"  [cyan]{m.session_id}[/]  {when}  [dim]{title}[/]")


def _pair_turns(records: list[dict]) -> list[ChatTurn]:
    """Reconstruct (user, assistant) turns from a transcript record stream."""
    turns: list[ChatTurn] = []
    pending_user: str | None = None
    pending_slot = "chat"
    for r in records:
        kind = r.get("kind")
        if kind == "user":
            # If a previous user had no assistant reply, flush it first.
            if pending_user is not None:
                turns.append(ChatTurn(user=pending_user, slot=pending_slot))
            pending_user = r.get("text", "")
            pending_slot = r.get("slot", "chat")
        elif kind == "assistant":
            turns.append(ChatTurn(
                user=pending_user or "",
                assistant=r.get("text", ""),
                slot=pending_slot,
                run_id=r.get("run_id", ""),
            ))
            pending_user = None
    if pending_user is not None:
        turns.append(ChatTurn(user=pending_user, slot=pending_slot))
    return turns


def resume_into(session_id: str, session: ChatSession, console: Console) -> bool:
    """Load a prior session's turns into `session` and replay them. Returns
    True on success."""
    loaded = session_store.load_session(session_id)
    if loaded is None:
        console.print(f"[yellow]No session {session_id!r}.[/]")
        return False
    meta, records = loaded
    turns = _pair_turns(records)

    console.print(f"[bold]Resuming session[/] [cyan]{session_id}[/] "
                  f"[dim]({len(turns)} turns)[/]")
    for t in turns:
        if t.user:
            console.print(f"[bold]› {t.user}[/]")
        if t.assistant:
            preview = t.assistant.strip().splitlines()[0:1]
            console.print(f"[dim]  {preview[0] if preview else ''}[/]")
    console.print("[dim]· resumed context is summarized, not a verbatim restore[/]")

    session.turns.extend(turns)
    return True
