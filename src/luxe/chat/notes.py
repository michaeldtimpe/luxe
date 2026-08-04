"""Session working notes — luxe remembers what it did.

At the end of a session with a project attached (and on demand via `/note`),
one non-agentic `backend.chat` call distils the folded conversation into a few
bullets and splices them into the `luxe:notes` block of
`<repo>/.luxe/memory.md`, newest first, on a rolling window. Every later
session in that repo gets them injected as `<project_memory>`.

Three disciplines, all load-bearing:

- **It never blocks exit.** Any failure — backend down, Ctrl-C mid-call, an
  unwritable repo — is a SILENT skip. There is no retry. A distillation is a
  nicety; quitting is not.
- **It never destroys user text.** Writes go through
  `memory.project.splice_block`, which preserves every byte outside the
  block, and the rolling window is applied in Python.
- **It is chat-only.** The benchmark/maintain path has no session, no
  front-end, and never imports this module.

Writing `.luxe/memory.md` from a READ-ONLY session is sanctioned: it is luxe's
own state file, not the user's code, and the write is orchestrator-side Python
rather than an agent tool (chat.sdd; same precedent as
`gitkit.store.mirror_to_repo`). It is not a `/write` bypass.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from luxe.agents import prompts

logger = logging.getLogger(__name__)

BLOCK_NAME = "notes"
#: Per-entry write cap, enforced in Python — never by asking the model.
MAX_ENTRY_CHARS = 900
#: Rolling window: newest N entries, and a hard ceiling on the whole block.
MAX_ENTRIES = 5
MAX_BLOCK_CHARS = 1500
#: Below this many assistant turns there is nothing worth handing over.
MIN_TURNS = 2

_ENTRY_RE = re.compile(r"^### ", re.MULTILINE)
_TRUNC = " …[truncated]"


@dataclass
class NotesResult:
    written: Path | None = None
    text: str = ""
    skipped: str = ""       # non-empty = why nothing was written


def _answered_turns(session) -> int:
    return sum(1 for t in getattr(session, "turns", [])
               if (getattr(t, "assistant", "") or "").strip())


def skip_reason(session, cfg, *, on_demand: bool = False) -> str:
    """Why notes should NOT be written now ("" = go ahead).

    `/note` is an explicit invocation, so it bypasses the config toggle and
    the turn-count floor — the user asking for it IS the consent.
    """
    repo = getattr(session, "repo_path", "") or ""
    if not repo or getattr(session, "project_kind", "none") == "none":
        return "no project attached"
    if on_demand:
        return ""
    if not getattr(cfg, "notes", True):
        return "notes: false in chat.yaml"
    if _answered_turns(session) < MIN_TURNS:
        return f"fewer than {MIN_TURNS} answered turns"
    return ""


def cap(text: str, limit: int = MAX_ENTRY_CHARS) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[: max(0, limit - len(_TRUNC))].rstrip() + _TRUNC


def _digest(session, *, budget_chars: int = 6000) -> str:
    """The distillation INPUT: the deterministic fold, never the raw
    transcript (chat.sdd — the summarizer is the one place conversation
    compaction lives, and it is versioned)."""
    from luxe.chat import summarize

    pairs = [((t.user or ""), (t.assistant or ""))
             for t in getattr(session, "turns", [])]
    return summarize.fold_history(pairs, budget_chars=budget_chars,
                                  keep_recent=4, older_cap=500)


def distil(session, backend) -> str:
    """One `backend.chat` call → the bullets. Returns "" on any failure."""
    body = _digest(session)
    if not body.strip():
        return ""
    messages = [
        {"role": "system", "content": prompts.SESSION_NOTES_HINT},
        {"role": "user",
         "content": f"<session_transcript>\n{body}\n</session_transcript>"},
    ]
    resp = backend.chat(messages, max_tokens=512, temperature=0.2)
    return (getattr(resp, "content", "") or "").strip()


def _entries(block: str | None) -> list[str]:
    """Existing entries, newest first, as whole `### …` chunks."""
    if not block:
        return []
    parts = _ENTRY_RE.split(block)
    return [f"### {p.strip()}" for p in parts if p.strip()]


def roll(existing: list[str], new_entry: str, *,
         max_entries: int = MAX_ENTRIES,
         max_chars: int = MAX_BLOCK_CHARS) -> str:
    """Newest-first rolling window: prepend, then drop the oldest until both
    the count and the total-character budget fit. Pure — unit-tested."""
    kept = [new_entry.strip(), *existing][:max_entries]
    while len(kept) > 1 and len("\n\n".join(kept)) > max_chars:
        kept.pop()
    return "\n\n".join(kept).strip()


def write_notes(repo_root: str | Path, bullets: str, session_id: str) -> Path:
    """Splice `bullets` in as a new dated entry under the rolling window."""
    from luxe.memory import project as project_mem

    path = project_mem.repo_memory_file(repo_root)
    existing_text = path.read_text(encoding="utf-8") if path.is_file() else ""
    block = project_mem.read_block(existing_text, BLOCK_NAME)
    entry = (f"### {time.strftime('%Y-%m-%d')} · {(session_id or '?')[:8]}\n"
             f"{cap(bullets)}")
    return project_mem.splice_block(
        repo_root, BLOCK_NAME, roll(_entries(block), entry),
        stamp="(session working notes, newest first — luxe rewrites this "
              "block; edit freely above/below)")


def run_session_notes(session, slots, cfg, console, *,
                      on_demand: bool = False) -> NotesResult:
    """Distil and write, or skip. NEVER raises, never blocks exit.

    Called from both front-ends' session-end `finally` (BEFORE the model
    unload — the backend is still usable there) and from `/note`.
    """
    why = skip_reason(session, cfg, on_demand=on_demand)
    if why:
        if on_demand:
            console.print(f"[yellow]· no session notes: {why}[/]")
        return NotesResult(skipped=why)
    try:
        backend = slots.backend_for("chat")
        bullets = distil(session, backend)
        if not bullets:
            return NotesResult(skipped="the model produced nothing")
        path = write_notes(session.repo_path, bullets, session.session_id)
    except SystemExit:
        raise
    except BaseException as e:   # noqa: BLE001 — see below
        # BaseException on purpose: Ctrl-C during the call arrives as
        # KeyboardInterrupt (and ChatCancelled is one), and anyio cancel
        # scopes raise BaseException subclasses too. Quitting must never be
        # held hostage by a nicety — silent skip, no retry.
        logger.info("session notes skipped: %s: %s", type(e).__name__, e)
        return NotesResult(skipped=f"{type(e).__name__}: {e}")
    console.print(f"[dim]· session notes → {path} "
                  "(disable: `notes: false` in chat.yaml)[/]")
    return NotesResult(written=path, text=bullets)
