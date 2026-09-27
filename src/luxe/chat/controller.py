"""Session lifecycle for `luxe chat`, independent of the front-end.

Both front-ends (`repl.py` line REPL, `tui.py` Textual app) call these;
moved verbatim out of `repl.py`, which re-exports every name.
"""

from __future__ import annotations

import logging

from luxe import ephemeral
from luxe.chat import origin as origin_mod
from luxe.memory import project as project_mem
from luxe.memory import session as session_store

# Name kept from the module these lines moved out of (debug.log unchanged).
logger = logging.getLogger("luxe.chat.repl")


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
