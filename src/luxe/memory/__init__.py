"""Durable session + project memory for the chat front-end.

Two concerns, kept separate (see memory.sdd):
  - session: per-conversation transcripts under ~/.luxe/sessions/<id>/ that can
    be replayed/resumed.
  - project: durable cross-session facts, injected at session start. Repo-local
    `.luxe/memory.md` is user-curated and always injected; auto-captured facts
    live unpromoted until the user promotes them.

Memory is injected ONLY via `run_single(extra_context=...)`. This package must
never read/write ~/.claude/ or the repo-root CLAUDE.md (Claude Code's own
memory).
"""

from __future__ import annotations

__all__ = [
    "SessionMeta",
    "new_session",
    "append_turn",
    "load_session",
    "list_sessions",
    "gc_sessions",
    "ProjectMemory",
    "load_memory",
    "add_fact",
    "promote_fact",
    "project_hash",
    "render_block",
]

from luxe.memory.project import (
    ProjectMemory,
    add_fact,
    load_memory,
    project_hash,
    promote_fact,
    render_block,
)
from luxe.memory.session import (
    SessionMeta,
    append_turn,
    gc_sessions,
    list_sessions,
    load_session,
    new_session,
)
