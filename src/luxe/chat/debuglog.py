"""Always-on per-session debug log for the interactive front-ends.

Every `luxe chat` / `luxe code` session writes `~/.luxe/sessions/<id>/debug.log`
from startup to teardown. Rationale (2026-07-30 fallback-kit pivot): the chat
TUI owns the screen, so stderr — and with it every logger.error traceback and
backend retry warning — was simply lost. When the fallback tool misbehaves
during an outage, "what happened" must be answerable after the fact without
having had foresight.

Shape: one `logging.FileHandler` attached to the `luxe` package logger (NOT
root — Textual's own loggers would flood the file; NOT a StreamHandler —
stderr writes corrupt the Textual screen). The handler is installed once the
session id exists and removed in the front-end's `finally`. Everything under
`luxe.*` that logs (backend retries, fswalk skips, turn crashes) lands in the
file with timestamps. Guarded throughout: a broken debug log must never take
down a session.
"""

from __future__ import annotations

import logging
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class SessionLog:
    """Handle for one session's debug-log installation (see install())."""

    def __init__(self, handler: logging.Handler | None,
                 prior_level: int, path: Path | None) -> None:
        self._handler = handler
        self._prior_level = prior_level
        self.path = path


def install(session_dir: str | Path) -> SessionLog:
    """Attach a DEBUG file handler for this session. Never raises."""
    pkg = logging.getLogger("luxe")
    prior = pkg.level
    try:
        path = Path(session_dir) / "debug.log"
        handler = logging.FileHandler(path, encoding="utf-8", delay=True)
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.setLevel(logging.DEBUG)
        pkg.addHandler(handler)
        # The package logger must pass records down to the handler; restore
        # the prior level at uninstall so we never leak config to the bench CLI.
        if pkg.level == logging.NOTSET or pkg.level > logging.DEBUG:
            pkg.setLevel(logging.DEBUG)
        return SessionLog(handler, prior, path)
    except Exception:
        return SessionLog(None, prior, None)


def uninstall(log: SessionLog) -> None:
    """Detach the session handler. Never raises."""
    if log is None or log._handler is None:
        return
    pkg = logging.getLogger("luxe")
    try:
        pkg.removeHandler(log._handler)
        log._handler.close()
        pkg.setLevel(log._prior_level)
    except Exception:
        pass
