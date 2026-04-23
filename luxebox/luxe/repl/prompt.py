"""Styled prompt helpers — `>>>` arrows, PromptSession setup, sub-prompts."""

from __future__ import annotations

from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings


def _styled_arrow_prompt(lead: str) -> FormattedText:
    """Build a FormattedText prompt with `<lead> >>> ` where the three
    arrows each pick an independent color from the palette and no two
    adjacent arrows share a color. Shared between the main `luxe` prompt
    and sub-prompts (`plan`, `role`, clarifying questions, save name)
    so the whole REPL reads consistently."""
    from luxe.repl.status import _pick_no_adjacent_repeats
    colors = _pick_no_adjacent_repeats(3)
    return FormattedText([
        ("", f"{lead} "),
        (f"bold fg:{colors[0]}", ">"),
        (f"bold fg:{colors[1]}", ">"),
        (f"bold fg:{colors[2]}", ">"),
        ("", " "),
    ])


def _prompt_message(sticky_agent: str) -> FormattedText:
    """Main-REPL prompt: leading newline for breathing room + `luxe` or
    `luxe (<mode>)`, then the colored `>>>`."""
    lead = f"\nluxe ({sticky_agent})" if sticky_agent else "\nluxe"
    return _styled_arrow_prompt(lead)


# Sub-prompt histories are kept in memory per label so ↑/↓ inside a
# plan-review loop or across clarifying answers works, but we don't
# pollute the main luxe>>> FileHistory. Lives for the luxe process
# lifetime; cleared on REPL exit.
_SUB_PROMPT_HISTORIES: dict[str, InMemoryHistory] = {}


def _ask_styled(lead: str) -> str:
    """One-shot styled sub-prompt — same colored `>>>` look as the main
    prompt. Keeps a per-label in-memory history so ↑/↓ recalls earlier
    entries within the same luxe session (plan commands stay in plan's
    buffer; save filenames in save's; etc.)."""
    from prompt_toolkit import prompt as _ptk_prompt
    key = lead.strip().lower()
    history = _SUB_PROMPT_HISTORIES.setdefault(key, InMemoryHistory())
    try:
        return _ptk_prompt(_styled_arrow_prompt(lead), history=history).strip()
    except (EOFError, KeyboardInterrupt):
        raise


def _make_prompt_session() -> PromptSession:
    """prompt_toolkit session with persistent history, arrow-key recall,
    and Alt/Esc+Enter to insert a newline inside a single prompt.

    Bracketed paste is on by default — pasting a multi-line block arrives
    as one buffer instead of N separate submissions.
    """
    hist_path = Path.home() / ".luxe" / "history"
    hist_path.parent.mkdir(parents=True, exist_ok=True)

    kb = KeyBindings()

    @kb.add("escape", "enter")  # Alt-Enter / Esc-Enter inserts a newline
    def _(event) -> None:  # noqa: ANN001
        event.current_buffer.insert_text("\n")

    return PromptSession(
        history=FileHistory(str(hist_path)),
        key_bindings=kb,
        enable_history_search=True,
        mouse_support=False,
    )
