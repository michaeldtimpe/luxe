"""Display-side text shaping that is not tied to any front-end.

Neutral tier: the on-screen truncation primitive is used by chat's final-answer
renderer AND by gitkit's report preview, and gitkit was importing it from
`chat.render` inside a function body to dodge the cycle. `chat.render`
re-exports it, so existing imports keep working.
"""

from __future__ import annotations


def truncate_for_display(text: str, *, max_lines: int | None,
                         max_chars: int | None = None) -> tuple[str, int]:
    """Truncate `text` for on-screen display, returning (shown, hidden_lines).

    Markdown-safe: truncates on raw text lines (before Markdown is evaluated),
    and if the kept slice leaves a code fence (```) open, appends a closing
    fence so the rest of the terminal output isn't swallowed. Returns (text, 0)
    unchanged when it already fits within both caps. Single shared primitive —
    used by `render_final` (chat) and the gitkit report preview.
    """
    text = text or ""
    lines = text.split("\n")
    cut = len(lines)
    if max_lines is not None and len(lines) > max_lines:
        cut = max_lines
    if max_chars is not None:
        total = 0
        for i, ln in enumerate(lines):
            total += len(ln) + 1
            if total > max_chars:
                cut = min(cut, i)
                break
    if cut >= len(lines):
        return text, 0
    kept = lines[:cut]
    hidden = len(lines) - cut
    if sum(1 for ln in kept if ln.lstrip().startswith("```")) % 2 == 1:
        kept.append("```")  # close a dangling code fence
    return "\n".join(kept), hidden
