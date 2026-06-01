"""Rich rendering for the chat REPL: live tool lines, final markdown, footer.

The agent loop already fires `on_tool_event(ToolCall)` after each dispatch
(loop.py) — we adapt that into live console lines without touching the loop.
Cancellation rides the same seam: when a Ctrl-C has set the CancelToken, the
adapter raises `ChatCancelled` at the next tool boundary, unwinding the turn
cleanly (KeyboardInterrupt is BaseException, so the loop's `except Exception`
guards don't swallow it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.markdown import Markdown

from luxe.tools.base import ToolCall


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


def render_footer(
    console: Console,
    *,
    slot: str,
    model: str,
    write_enabled: bool,
    result,  # AgentResult
    swap_count: int = 0,
    swap_seconds: float = 0.0,
) -> None:
    mode = "[yellow]write[/]" if write_enabled else "[green]read-only[/]"
    bits = [
        f"slot: {slot}",
        f"model: {model}",
        f"mode: {mode}",
        f"steps: {result.steps}",
        f"tools: {result.tool_calls_total}",
        f"{result.wall_s:.1f}s",
        f"tok: {result.prompt_tokens}+{result.completion_tokens}",
        f"ctx: {result.peak_context_pressure:.0%}",
    ]
    if swap_count:
        bits.append(f"swaps: {swap_count} ({swap_seconds:.0f}s)")
    console.print("[dim]· " + " · ".join(bits) + "[/]")
