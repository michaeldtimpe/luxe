"""Rich rendering for the chat REPL: live tool lines, final markdown, footer.

The agent loop already fires `on_tool_event(ToolCall)` after each dispatch
(loop.py) — we adapt that into live console lines without touching the loop.
Cancellation rides the same seam: when a Ctrl-C has set the CancelToken, the
adapter raises `ChatCancelled` at the next tool boundary, unwinding the turn
cleanly (KeyboardInterrupt is BaseException, so the loop's `except Exception`
guards don't swallow it).
"""

from __future__ import annotations

import difflib
import random
import re
import time
from dataclasses import dataclass
from typing import Any

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.markup import escape as _escape
from rich.padding import Padding
from rich.syntax import Syntax
from rich.text import Text

from luxe.chat import theme as theme_mod
from luxe.tools.base import ToolCall

# Shared 6-color palette for the rainbow banner and the prompt arrows. Switched
# from fixed hex (which ignored the user's theme) to ANSI color *names* (B4) so
# every color renders in the terminal/iTerm ANSI profile and tracks the active
# theme. Two spellings: Rich tags for markup, prompt_toolkit tokens for the ptk
# arrow prompt.
PROMPT_ARROW_PALETTE = ["red", "yellow", "green", "cyan", "blue", "magenta"]
ARROW_PALETTE_PTK = ["ansired", "ansiyellow", "ansigreen",
                     "ansicyan", "ansiblue", "ansimagenta"]


def pick_no_adjacent_repeats(
    n: int, *, rng: random.Random | None = None, palette: list[str] | None = None,
) -> list[str]:
    """Pick `n` colors from `palette` (default: the Rich arrow palette) with no
    two neighbors equal. Shared between the rainbow banner and the prompt arrows.
    `rng` is injectable for deterministic tests; `palette` lets the ptk reader
    pass its own `ansi*` tokens.
    """
    pal = palette or PROMPT_ARROW_PALETTE
    chooser = (rng or random).choice
    picks: list[str] = []
    for _ in range(n):
        pool = [c for c in pal if not picks or c != picks[-1]]
        picks.append(chooser(pool))
    return picks


def rainbow_banner(label: str = "luxe chat", *, rng: random.Random | None = None) -> str:
    """Rich-markup `.:. <label> .:.` — each of the 6 punctuation chars picks an
    independent palette color (no adjacent duplicates); the label stays white."""
    colors = pick_no_adjacent_repeats(6, rng=rng)
    marker = (".", ":", ".")
    left = "".join(f"[{colors[i]}]{c}[/]" for i, c in enumerate(marker))
    right = "".join(f"[{colors[i + 3]}]{c}[/]" for i, c in enumerate(marker))
    return f"{left} [bold white]{label}[/] {right}"


def arrow_prompt_markup(lead: str = "luxe", *, rng: random.Random | None = None) -> str:
    """Rich-markup `<lead> ›››` with three independently-colored arrows (no two
    adjacent the same). Used by the plain-input reader fallback; prompt_toolkit
    builds its own FormattedText from `pick_no_adjacent_repeats`."""
    colors = pick_no_adjacent_repeats(3, rng=rng)
    arrows = "".join(f"[bold {c}]›[/]" for c in colors)
    return f"{lead} {arrows} "


class ChatCancelled(KeyboardInterrupt):
    """Raised at a tool boundary when the user requested cancellation."""


@dataclass
class CancelToken:
    requested: bool = False

    def reset(self) -> None:
        self.requested = False


# Args most worth surfacing first when summarizing a tool call.
_SALIENT_ARGS = ("path", "file", "query", "pattern", "symbol", "command", "message")


def summarize_args(args: dict[str, Any], *, max_len: int = 60) -> str:
    if not args:
        return ""
    key = next((k for k in _SALIENT_ARGS if k in args), None)
    if key is None:
        key = next(iter(args))
    val = str(args.get(key, ""))
    val = val.replace("\n", " ")
    if len(val) > max_len:
        val = val[: max_len - 1] + "…"
    return f"{key}={val!r}"


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def format_tool_call(tc: ToolCall) -> str:
    """One-line Rich-markup summary of a dispatched tool call. Colors come from
    theme roles (B4) so they track the user's terminal/YASL theme."""
    accent = theme_mod.rich("accent") or "cyan"
    # Escape the args so a value containing `[` can't break the span. The PRINT
    # sites pass highlight=False so rich's ReprHighlighter doesn't repaint the
    # untagged `name(` call-pattern magenta over the theme (iter-6 color fix).
    head = f"[{accent}]→[/] {tc.name}([dim]{_escape(summarize_args(tc.arguments))}[/])"
    if tc.error:
        tail = f"  [{theme_mod.rich('error') or 'red'}]✗ {tc.error[:80]}[/]"
    elif tc.duplicate:
        tail = f"  [{theme_mod.rich('warn') or 'yellow'}]⟳ duplicate[/]"
    elif tc.cached:
        tail = "  [dim]⟳ cached[/]"
    else:
        tail = f"  [{theme_mod.rich('success') or 'green'}]✓[/] [dim]{_human_bytes(tc.bytes_out)}[/]"
    return head + tail


def raise_if_cancelled(cancel: CancelToken) -> None:
    """Raise ChatCancelled if a Ctrl-C has set the token. Shared by the tool
    boundary and the streaming token callback (B1) so cancellation lands
    mid-generation, not only between tool calls."""
    if cancel.requested:
        raise ChatCancelled()


# Generous caps so a write-heavy turn can't lock the terminal. `diff` keeps each
# block tight; `full` is the escape hatch and still bounded to avoid a runaway.
_VERBOSE_CAP = {"diff": 4096, "full": 200_000}


def _capped_block(text: str, cap: int, *, indent: str = "    ") -> str:
    """Escape Rich markup, indent, and truncate `text` to `cap` chars with a
    `… +N more lines` footer so verbose output stays bounded."""
    text = text or ""
    truncated = len(text) > cap
    shown = text[:cap]
    lines = shown.splitlines() or [""]
    body = "\n".join(f"{indent}[dim]{_escape(ln)}[/]" for ln in lines)
    if truncated:
        remaining = text[cap:].count("\n") + 1
        body += f"\n{indent}[dim]… +{remaining} more line(s) (use /verbose full)[/]"
    return body


def _unified_diff(path: str, old: str, new: str, cap: int) -> str:
    diff = difflib.unified_diff(
        (old or "").splitlines(), (new or "").splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
    )
    add = theme_mod.rich("diff_add") or "green"
    rem = theme_mod.rich("diff_del") or "red"
    hunk = theme_mod.rich("diff_hunk") or "cyan"
    out: list[str] = []
    for ln in diff:
        if ln.startswith("+") and not ln.startswith("+++"):
            out.append(f"    [{add}]{_escape(ln)}[/]")
        elif ln.startswith("-") and not ln.startswith("---"):
            out.append(f"    [{rem}]{_escape(ln)}[/]")
        elif ln.startswith("@@"):
            out.append(f"    [{hunk}]{_escape(ln)}[/]")
        else:
            out.append(f"    [dim]{_escape(ln)}[/]")
    text = "\n".join(out)
    if len(text) > cap:
        text = text[:cap] + "\n    [dim]… diff truncated (use /verbose full)[/]"
    return text or "    [dim](no textual change)[/]"


# File extension → pygments lexer for syntax-highlighted code blocks (B3).
_LEXER_BY_EXT = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "jsx",
    ".ts": "typescript", ".tsx": "tsx", ".json": "json", ".toml": "toml",
    ".yaml": "yaml", ".yml": "yaml", ".md": "markdown", ".sh": "bash",
    ".bash": "bash", ".zsh": "bash", ".rs": "rust", ".go": "go", ".rb": "ruby",
    ".java": "java", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp",
    ".html": "html", ".css": "css", ".sql": "sql", ".toml1": "toml",
}


def _lexer_for(path: str) -> str:
    import os
    return _LEXER_BY_EXT.get(os.path.splitext(path)[1].lower(), "text")


def _code_block(code: str, lexer: str, cap: int):
    """Syntax-highlighted, left-padded code block. `ansi_dark` maps to ANSI
    colors so it tracks the terminal profile (B4 spirit); bg stays the terminal
    default so it doesn't fight the user's theme."""
    code = code if len(code) <= cap else code[:cap] + "\n… (truncated)"
    syn = Syntax(code, lexer, theme="ansi_dark", background_color="default",
                 word_wrap=False)
    return Padding(syn, (0, 0, 0, 2))


def _diff_block(path: str, old: str, new: str, cap: int):
    """Unified diff rendered with the `diff` lexer — added/removed lines are
    highlighted (green/red), hunks cyan — and left-padded."""
    diff = "\n".join(difflib.unified_diff(
        (old or "").splitlines(), (new or "").splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))
    if not diff.strip():
        return Padding(Text("(no textual change)", style="dim"), (0, 0, 0, 2))
    if len(diff) > cap:
        diff = diff[:cap] + "\n… (diff truncated; /verbose full)"
    syn = Syntax(diff, "diff", theme="ansi_dark", background_color="default",
                 word_wrap=False)
    return Padding(syn, (0, 0, 0, 2))


def _capped_text(body: str, cap: int) -> Text:
    body = body or ""
    truncated = len(body) > cap
    shown = body[:cap]
    t = Text(shown, style="dim")
    if truncated:
        remaining = body[cap:].count("\n") + 1
        t.append(f"\n… +{remaining} more line(s) (/verbose full)", style="dim")
    return t


def format_tool_call_verbose(tc: ToolCall, level: str):
    """Rich renderable for a dispatched tool call under /verbose (B3).

    `level` is "diff" (edits as a highlighted unified diff, file writes as a
    header, bodies capped) or "full" (whole file contents, syntax-highlighted).
    Returns a rich Group; wrapped in crash-safety so a malformed diff/lexer can't
    break the turn — it falls back to plain styled text.
    """
    try:
        return _verbose_group(tc, level)
    except Exception:
        return _verbose_fallback(tc, level)


def _verbose_group(tc: ToolCall, level: str):
    cap = _VERBOSE_CAP.get(level, _VERBOSE_CAP["diff"])
    args = tc.arguments or {}
    # Head: the one-liner with wall-time folded in (no separate line).
    head = Text.from_markup(format_tool_call(tc)
                            + f"  [dim]{tc.wall_s * 1000:.0f}ms[/]")
    items: list = [head]

    if tc.name == "edit_file" and not tc.error:
        path = str(args.get("path", "?"))
        items.append(_diff_block(path, str(args.get("old_string", "")),
                                 str(args.get("new_string", "")), cap))
    elif tc.name == "write_file" and not tc.error:
        path = str(args.get("path", "?"))
        content = str(args.get("content", ""))
        nlines = content.count("\n") + 1 if content else 0
        items.append(Text.from_markup(
            f"    [dim]write {_escape(path)} ({nlines} lines)[/]"))
        if level == "full" and content:
            items.append(_code_block(content, _lexer_for(path), cap))
    else:
        for k, v in args.items():
            sval = str(v)
            if "\n" in sval or len(sval) > 80:
                items.append(Text.from_markup(f"    [dim]{_escape(k)}:[/]"))
                items.append(_capped_text(sval, cap))
            else:
                items.append(Text.from_markup(
                    f"    [dim]{_escape(k)}={_escape(sval)}[/]"))

    if tc.error:
        items.append(Text.from_markup(f"    [{theme_mod.rich('error') or 'red'}]error[/]"))
        items.append(_capped_text(tc.error, cap))
    elif tc.result and tc.name != "write_file":
        items.append(Text.from_markup("    [dim]result[/]"))
        items.append(_capped_text(tc.result, cap))
    return Group(*items)


def _verbose_fallback(tc: ToolCall, level: str) -> str:
    """Plain-text fallback (also the crash-safe path): no Syntax, just styled
    markup with basic +/- diff colors."""
    cap = _VERBOSE_CAP.get(level, _VERBOSE_CAP["diff"])
    lines = [format_tool_call(tc) + f"  [dim]{tc.wall_s * 1000:.0f}ms[/]"]
    args = tc.arguments or {}
    if tc.name == "edit_file" and not tc.error:
        lines.append(_unified_diff(str(args.get("path", "?")),
                                   str(args.get("old_string", "")),
                                   str(args.get("new_string", "")), cap))
    elif tc.name == "write_file" and not tc.error:
        content = str(args.get("content", ""))
        nlines = content.count("\n") + 1 if content else 0
        lines.append(f"    [dim]write {_escape(str(args.get('path', '?')))} "
                     f"({nlines} lines)[/]")
        if level == "full":
            lines.append(_capped_block(content, cap))
    if tc.error:
        lines.append("    [red]error[/]")
        lines.append(_capped_block(tc.error, cap))
    elif tc.result and tc.name != "write_file":
        lines.append("    [dim]result[/]")
        lines.append(_capped_block(tc.result, cap))
    return "\n".join(lines)


def make_tool_event(console: Console, cancel: CancelToken,
                    verbose_level: str = "off"):
    """Build an `on_tool_event` callback that renders live + honors cancel."""

    def _on_event(tc: ToolCall) -> None:
        if verbose_level in ("diff", "full"):
            console.print(format_tool_call_verbose(tc, verbose_level))
        else:
            # highlight=False keeps markup ON but stops the ReprHighlighter from
            # repainting the tool name magenta over the theme (iter-6).
            console.print(format_tool_call(tc), highlight=False)
        raise_if_cancelled(cancel)

    return _on_event


_RE_BLANK_RUN = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


def render_final(console: Console, text: str) -> None:
    text = (text or "").strip()
    if not text:
        console.print("[dim](no response text)[/]")
        return
    # D3: collapse runs of 2+ blank lines to a single blank line so the model's
    # spread-out "thinking" doesn't stack with rich Markdown's paragraph spacing.
    text = _RE_BLANK_RUN.sub("\n\n", text)
    console.print(Markdown(text))


def _tok_per_s(result) -> float:
    """Honest generation rate: completion tokens over model wall (0 if no wall)."""
    if result.wall_s <= 0:
        return 0.0
    return result.completion_tokens / result.wall_s


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def render_footer(
    console: Console,
    *,
    slot: str,
    model: str,
    write_enabled: bool,
    result,  # AgentResult
    swap_count: int = 0,
    swap_seconds: float = 0.0,
    started_at: float | None = None,
    ended_at: float | None = None,
) -> None:
    mode = (f"[{theme_mod.rich('warn') or 'yellow'}]write[/]" if write_enabled
            else f"[{theme_mod.rich('success') or 'green'}]read-only[/]")
    bits = [
        f"slot: {slot}",
        f"model: {model}",
        f"mode: {mode}",
        f"steps: {result.steps}",
        f"tools: {result.tool_calls_total}",
        f"{result.wall_s:.1f}s",
        f"{_tok_per_s(result):.0f} tok/s",
        f"tok: {result.prompt_tokens}+{result.completion_tokens}",
        f"ctx(peak): {result.peak_context_pressure:.0%}",
    ]
    if swap_count:
        bits.append(f"swaps: {swap_count} ({swap_seconds:.0f}s)")
    console.print("[dim]· " + " · ".join(bits) + "[/]")
    # Wall-clock bookends + full-turn elapsed (covers overhead the model wall
    # above misses, and supplies a total duration when wall_s is unset).
    if started_at is not None and ended_at is not None:
        elapsed = max(0.0, ended_at - started_at)
        console.print(
            f"[dim]· started {_clock(started_at)} · ended {_clock(ended_at)}"
            f" · elapsed {elapsed:.1f}s[/]"
        )
