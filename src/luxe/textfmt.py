"""Display-side text shaping that is not tied to any front-end.

Neutral tier: the on-screen truncation primitive is used by chat's final-answer
renderer AND by gitkit's report preview, and gitkit was importing it from
`chat.render` inside a function body to dodge the cycle. `chat.render`
re-exports it, so existing imports keep working.
"""

from __future__ import annotations

from pathlib import Path


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


def tilde(path: str) -> str:
    """Home-relative display path (`~/Downloads/luxe`).

    Three copies of these two lines existed (chat launch, chat.origin,
    chat.status); this is the one. Purely cosmetic — it never touches the
    filesystem, so a path that does not exist renders the same way.
    """
    home = str(Path.home())
    return "~" + path[len(home):] if home and path.startswith(home) else path


def render_ok_lines(console, lines) -> None:
    """Print `(ok, text)` probe results as `  ✓ text` / `  ✗ text`.

    The shared render for netdiag and planeproxy reports, which each surface
    on BOTH the CLI (`luxe net`, `luxe planeproxy`) and in a session (`/net`,
    `/planeproxy`) — four identical loops before 2026-08-04. The probe modules
    return pure text on purpose and the markup belongs here.
    """
    for ok, line in lines:
        glyph = "[green]✓[/]" if ok else "[red]✗[/]"
        console.print(f"  {glyph} {line}")
