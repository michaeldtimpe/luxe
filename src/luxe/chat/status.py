"""Bottom-toolbar status bar for `luxe chat` (lightweight prompt_toolkit variant).

Renders a single static bar pinned under the input line while you type. It is
NOT live during a model turn (the tool tail-log streams above instead); it
refreshes between turns from `StatusState`, which the REPL updates after each
turn. Git branch/dirty is TTL-cached so it doesn't shell out on every keystroke.

`fields()` is the single source of truth for segment order + content; both the
prompt_toolkit toolbar and the plain-text fallback render from it, so the two
never drift. Swap the field list here to restyle the bar.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from luxe.chat.session import tier_label

_GIT_TTL = 5.0
_git_cache: dict[str, tuple[float, tuple[str, int] | None]] = {}


def git_status(repo: str) -> tuple[str, int] | None:
    """(branch, dirty_file_count) for `repo`, or None if not a git repo.
    TTL-cached so per-keystroke toolbar redraws don't spawn git each time."""
    if not repo:
        return None
    now = time.monotonic()
    hit = _git_cache.get(repo)
    if hit and (now - hit[0]) < _GIT_TTL:
        return hit[1]
    res: tuple[str, int] | None
    try:
        br = subprocess.run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, timeout=2)
        if br.returncode != 0:
            res = None
        else:
            st = subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                                capture_output=True, text=True, timeout=2)
            dirty = sum(1 for ln in st.stdout.splitlines() if ln.strip())
            res = (br.stdout.strip() or "HEAD", dirty)
    except Exception:
        res = None
    _git_cache[repo] = (now, res)
    return res


@dataclass
class StatusState:
    """Mutable snapshot of the last completed turn; updated by the REPL."""
    slot: str = "chat"
    model: str = ""
    wall_s: float = 0.0
    tok_per_s: float = 0.0
    ctx_pressure: float = 0.0
    steps: int = 0
    has_turn: bool = False  # False until the first turn completes


def _short_model(model: str) -> str:
    name = (model or "?").split("/")[-1]
    return name if len(name) <= 22 else name[:21] + "…"


def fields(session, slots, repo: str, state: StatusState) -> list[tuple[str, str, str]]:
    """Ordered status segments as (text, ptk_style, rich_style) tuples.

    THE place to change the bar's format. ptk_style is a prompt_toolkit style
    string; rich_style is the equivalent Rich markup tag (or "" for default).
    """
    out: list[tuple[str, str, str]] = []

    # --- mode chips -------------------------------------------------------
    if session.write_enabled:
        out.append((" WRITE ", "bg:#8a6d00 #ffffff bold", "black on yellow"))
        if session.unrestricted_bash:
            out.append((" BASH ", "bg:#8a1c1c #ffffff bold", "white on red"))
    else:
        out.append((" READ-ONLY ", "bg:#1f5c2f #ffffff bold", "white on green"))

    # --- slot:model -------------------------------------------------------
    model = state.model or slots.model_for("chat")
    out.append((f" {state.slot}:{_short_model(model)}", "", "cyan"))

    # --- context window ---------------------------------------------------
    tier = tier_label(session.num_ctx_override) if session.num_ctx_override else "default"
    ctx = f" ctx {tier}"
    if state.has_turn:
        ctx += f" {state.ctx_pressure:.0%}"
    out.append((ctx, "", "magenta"))

    # --- last turn timing/rate -------------------------------------------
    if state.has_turn:
        out.append((f" {state.wall_s:.1f}s {state.tok_per_s:.0f}tok/s",
                    "", "dim"))

    # --- git --------------------------------------------------------------
    gs = git_status(repo)
    if gs is not None:
        branch, dirty = gs
        out.append((f" ⎇ {branch}", "#88aaff", "blue"))
        if dirty:
            out.append((f" ●{dirty}", "#ffcc55 bold", "yellow"))
        else:
            out.append((" ✓", "#66e066", "green"))

    return out


def toolbar(session, slots, repo: str, state: StatusState):
    """prompt_toolkit bottom_toolbar value (FormattedText)."""
    from prompt_toolkit.formatted_text import FormattedText

    parts: list[tuple[str, str]] = []
    for i, (text, style, _rich) in enumerate(fields(session, slots, repo, state)):
        if i:
            parts.append(("", " "))
        parts.append((style, text))
    return FormattedText(parts)


def status_markup(session, slots, repo: str, state: StatusState) -> str:
    """Rich-markup one-liner for the plain-input fallback (no prompt_toolkit)."""
    chunks = []
    for text, _style, rich in fields(session, slots, repo, state):
        chunks.append(f"[{rich}]{text.strip()}[/]" if rich else text.strip())
    return "[dim]· " + " · ".join(chunks) + "[/]"
