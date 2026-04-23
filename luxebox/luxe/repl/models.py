"""Model management helpers for /variants, /pull, and post-pull role assignment."""

from __future__ import annotations

import httpx

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from luxe.backend import (
    MODEL_VARIANTS,
    clear_caches,
    installed_by_family,
    list_models,
    pull_stream,
)
from luxe.registry import LuxeConfig

console = Console()


def _print_variants(family_filter: str | None, cfg: LuxeConfig) -> None:
    """Render installed vs released variants per model family."""
    installed = list_models(cfg.ollama_base_url)
    grouped = installed_by_family(installed)

    families = [family_filter] if family_filter else sorted(
        set(grouped) | set(MODEL_VARIANTS)
    )

    t = Table(show_header=True, box=None, padding=(0, 2), header_style="dim")
    t.add_column("family")
    t.add_column("installed")
    t.add_column("available")
    for family in families:
        have = grouped.get(family, set())
        released = MODEL_VARIANTS.get(family)
        if not have and released is None:
            continue  # unknown family and nothing installed → skip
        inst_display = ", ".join(sorted(have)) if have else "[dim]—[/dim]"
        if released:
            # Green for installed, dim for pullable.
            avail_display = ", ".join(
                f"[green]{v}[/green]" if v in have else f"[dim]{v}[/dim]"
                for v in released
            )
        else:
            avail_display = "[dim]?[/dim]"
        t.add_row(family, inst_display, avail_display)
    console.print(t)
    console.print(
        "[dim]green = installed · dim = available via[/dim] [cyan]/pull <family>:<size>[/cyan]"
    )


def _pull_with_progress(tag: str, base_url: str) -> bool:
    """Stream Ollama pull and render per-layer progress bars. Returns True
    on clean success. Ctrl-C aborts gracefully."""
    console.print(f"[dim]pulling[/dim] [cyan]{tag}[/cyan][dim]...[/dim]")
    with Progress(
        TextColumn("[dim]{task.description}"),
        BarColumn(bar_width=30),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        tasks: dict[str, int] = {}
        try:
            for event in pull_stream(tag, base_url):
                if err := event.get("error"):
                    progress.console.print(f"[red]error:[/red] {err}")
                    return False
                digest = event.get("digest", "")
                total = event.get("total")
                completed = event.get("completed") or 0
                if digest and total:
                    if digest not in tasks:
                        tasks[digest] = progress.add_task(digest[7:19], total=total)
                    progress.update(tasks[digest], completed=completed)
                else:
                    status = (event.get("status") or "").strip()
                    # Layer lines come with digest — skip those we didn't bind;
                    # surface only the top-level status messages.
                    if status and not status.startswith("pulling ") and status != "success":
                        progress.console.print(f"[dim]· {status}[/dim]")
        except KeyboardInterrupt:
            progress.console.print("[yellow]pull interrupted[/yellow]")
            return False
        except httpx.HTTPError as e:
            progress.console.print(f"[red]pull failed:[/red] {e}")
            return False
    # Fresh weights → invalidate cached ctx + params for this tag so the
    # banner and /context pick up the real values on the next lookup.
    clear_caches(tag)
    console.print(f"[green]✓ pulled[/green] [cyan]{tag}[/cyan]")
    return True


def _prompt_assign_to_agent(tag: str, cfg: LuxeConfig) -> None:
    """After a successful pull, ask if the user wants to wire the new model
    into an agent role. In-memory only for this session — persistence
    requires editing configs/agents.yaml."""
    from luxe.repl.prompt import _ask_styled
    roles = [a.name for a in cfg.agents]
    console.print(
        f"\n[dim]Assign[/dim] [cyan]{tag}[/cyan] [dim]to an agent role? Options:[/dim] "
        f"{', '.join(roles)} [dim]or[/dim] skip"
    )
    try:
        answer = _ask_styled("role").lower()
    except (EOFError, KeyboardInterrupt):
        console.print("[dim]skipped[/dim]")
        return
    if not answer or answer == "skip":
        return
    if answer not in roles:
        console.print(f"[yellow]not an agent name:[/yellow] {answer} [dim](skipped)[/dim]")
        return
    agent = cfg.get(answer)
    old = agent.model
    agent.model = tag
    console.print(
        f"[dim]→[/dim] [cyan]{answer}[/cyan] [dim]model:[/dim] "
        f"[dim]{old}[/dim] [dim]→[/dim] [cyan]{tag}[/cyan]"
    )
    console.print(
        "[dim]  (session-only · edit configs/agents.yaml to persist across restarts)[/dim]"
    )
