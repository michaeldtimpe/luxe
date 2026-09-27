"""`luxe chat` / `luxe code` — thin Click shells over chat/launch.py."""

from __future__ import annotations

from luxe.cli._common import main


# `luxe chat` / `luxe code` — thin Click shells. The shared option list, the
# posture wiring, and the whole startup path live in chat/launch.py. The
# non-shell names are re-exported (here, and again by `luxe.cli`) so
# `luxe.cli._X` keeps resolving for the tests and scripts that import them
# there.
from luxe.chat.launch import (  # noqa: F401  (re-exports)
    _INDEX_MAX_FILES,
    _INDEX_MAX_MB,
    _apply_slot_overrides,
    _build_chat_indexes,
    _resolve_theme_name,
    _run_interactive,
    _shared_chat_options,
    _tilde,
)


@main.command(name="chat")
@_shared_chat_options
def chat_cmd(**kwargs):
    """Interactive terminal agent (Claude-CLI-style). Starts anywhere; default:
    the host manifest's main model in every slot, read-only tools (toggle with
    /write). Use `luxe code` for a project-first, write-on session."""
    _run_interactive(require_project=False, start_write=False, **kwargs)


@main.command(name="code")
@_shared_chat_options
def code_cmd(**kwargs):
    """Project coding session: same engine as `luxe chat`, different posture —
    REQUIRES a project (git root or marker directory; errors out otherwise)
    and starts with write tools ON. Bash stays gated (/bash to enable)."""
    _run_interactive(require_project=True, start_write=True, **kwargs)
