"""Rich rendering for the chat REPL: live tool lines, final markdown, footer.

The agent loop already fires `on_tool_event(ToolCall)` after each dispatch
(loop.py) — we adapt that into live console lines without touching the loop.
Cancellation rides the same seam: when a Ctrl-C has set the CancelToken, the
adapter raises `ChatCancelled` at the next tool boundary, unwinding the turn
cleanly (KeyboardInterrupt is BaseException, so the loop's `except Exception`
guards don't swallow it).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.markdown import Markdown

from luxe.tools.base import ToolCall

# Shared 6-color palette for the rainbow banner and the prompt arrows — left
# warm, right cool. Ported from the retired luxe_cli REPL so the chat front-end
# keeps the same playful, per-render color shift.
PROMPT_ARROW_PALETTE = [
    "#ff5c5c",  # red
    "#ffa040",  # orange
    "#ffdd33",  # yellow
    "#66d9ff",  # light blue
    "#66e066",  # green
    "#c38bff",  # violet
]


def pick_no_adjacent_repeats(n: int, *, rng: random.Random | None = None) -> list[str]:
    """Pick `n` colors from the palette with no two neighbors equal.

    Shared between the rainbow banner and the prompt arrows so the whole REPL
    reads consistently. `rng` is injectable for deterministic tests.
    """
    chooser = (rng or random).choice
    picks: list[str] = []
    for _ in range(n):
        pool = [c for c in PROMPT_ARROW_PALETTE if not picks or c != picks[-1]]
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
    """One-line Rich-markup summary of a dispatched tool call."""
    head = f"[cyan]→[/] {tc.name}([dim]{summarize_args(tc.arguments)}[/])"
    if tc.error:
        tail = f"  [red]✗ {tc.error[:80]}[/]"
    elif tc.duplicate:
        tail = "  [yellow]⟳ duplicate[/]"
    elif tc.cached:
        tail = "  [dim]⟳ cached[/]"
    else:
        tail = f"  [green]✓[/] [dim]{_human_bytes(tc.bytes_out)}[/]"
    return head + tail


def make_tool_event(console: Console, cancel: CancelToken):
    """Build an `on_tool_event` callback that renders live + honors cancel."""

    def _on_event(tc: ToolCall) -> None:
        console.print(format_tool_call(tc))
        if cancel.requested:
            raise ChatCancelled()

    return _on_event


def render_final(console: Console, text: str) -> None:
    text = (text or "").strip()
    if not text:
        console.print("[dim](no response text)[/]")
        return
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
    mode = "[yellow]write[/]" if write_enabled else "[green]read-only[/]"
    bits = [
        f"slot: {slot}",
        f"model: {model}",
        f"mode: {mode}",
        f"steps: {result.steps}",
        f"tools: {result.tool_calls_total}",
        f"{result.wall_s:.1f}s",
        f"{_tok_per_s(result):.0f} tok/s",
        f"tok: {result.prompt_tokens}+{result.completion_tokens}",
        f"ctx: {result.peak_context_pressure:.0%}",
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
