"""Rich presentation for compare: side-by-side panels, tool-set diff, vote.

The vote prompt always prints a non-determinism disclaimer first (compare.sdd):
the champion is not byte-deterministic at temp=0, so a single-task comparison is
qualitative, not a controlled benchmark.
"""

from __future__ import annotations

from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel

from luxe.compare.run_pair import CompareResult, SideResult
from luxe.compare import store as store_mod

_DISCLAIMER = (
    "[yellow]⚠ champion is non-deterministic at temp=0; this is a qualitative "
    "comparison, not a controlled benchmark.[/]"
)


def _metrics_line(s: SideResult, *, blind: bool) -> str:
    bits = [
        f"steps {s.steps}",
        f"tools {s.tool_calls_total}",
        f"{s.wall_s:.1f}s",
        f"tok {s.prompt_tokens}+{s.completion_tokens}",
    ]
    if not blind:
        bits.insert(0, f"model {s.model_id}")
        if s.substrate_env:
            bits.append("substrate off")
    if s.aborted:
        bits.append(f"[red]aborted: {s.abort_reason}[/]")
    return "[dim]" + " · ".join(bits) + "[/]"


def _panel(title: str, s: SideResult, *, blind: bool) -> Panel:
    body = (s.final_text or "[dim](no output)[/]").strip()
    footer = _metrics_line(s, blind=blind)
    return Panel(f"{body}\n\n{footer}", title=title, expand=True)


def render_side_by_side(console: Console, result: CompareResult, *, blind: bool | None = None) -> None:
    blind = result.blind if blind is None else blind
    a, b = result.sides[0], result.sides[1]
    if blind:
        ta, tb = "Left", "Right"
    else:
        ta = f"[bold]{a.label}[/]"
        tb = f"[bold]{b.label}[/]"
    console.print(Columns([_panel(ta, a, blind=blind), _panel(tb, b, blind=blind)], equal=True))
    _render_diff(console, a, b, blind=blind)


def _render_diff(console: Console, a: SideResult, b: SideResult, *, blind: bool) -> None:
    sa, sb = set(a.tool_names), set(b.tool_names)
    only_a = sorted(sa - sb)
    only_b = sorted(sb - sa)
    if only_a or only_b:
        la = "Left" if blind else a.label
        lb = "Right" if blind else b.label
        if only_a:
            console.print(f"[dim]tools only in {la}: {', '.join(only_a)}[/]")
        if only_b:
            console.print(f"[dim]tools only in {lb}: {', '.join(only_b)}[/]")


def prompt_vote(console: Console, result: CompareResult, *, reader=None) -> str | None:
    """Print the disclaimer, collect a vote + optional rationale, persist it.

    Returns the recorded winner side-label (de-anonymized), or None if skipped.
    """
    reader = reader or (lambda prompt: console.input(prompt))
    console.print(_DISCLAIMER)
    choice = reader("which is better? [left/right/tie/skip]: ").strip().lower()
    if choice in ("", "skip"):
        return None
    a, b = result.sides[0], result.sides[1]
    if choice in ("left", "l", "a"):
        winner = a.label
    elif choice in ("right", "r", "b"):
        winner = b.label
    elif choice == "tie":
        winner = "tie"
    else:
        console.print("[yellow]unrecognized choice; not recorded[/]")
        return None
    reason = reader("why? (optional): ").strip()
    store_mod.record_vote(result.compare_id, winner, reason=reason, blind=result.blind)
    console.print(f"[green]✓ recorded[/] winner={winner}"
                  + (f" · {reason}" if reason else ""))
    return winner
