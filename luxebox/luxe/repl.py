"""Interactive REPL. Reads prompts, routes, prints responses, logs sessions."""

from __future__ import annotations

import readline  # noqa: F401  — side-effect: enables arrow-key history
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from luxe import router, runner
from luxe.backend import list_models, ping
from luxe.registry import LuxeConfig
from luxe.session import Session

console = Console()

BANNER = """[bold cyan]luxe[/bold cyan] — local multi-agent CLI
type a prompt, or [dim]/help[/dim] for commands. [dim]Ctrl-D to exit.[/dim]"""

HELP = """[bold]Commands[/bold]
  [cyan]/help[/cyan]      show this message
  [cyan]/agents[/cyan]    list configured agents
  [cyan]/session[/cyan]   show current session path
  [cyan]/quit[/cyan]      exit
"""


def start(cfg: LuxeConfig, session: Session | None = None) -> None:
    if not ping():
        console.print(
            "[red]Ollama is not reachable at http://127.0.0.1:11434.[/red] "
            "Start it with [cyan]ollama serve[/cyan]."
        )
        return

    sess = session or Session.new(Path(cfg.session_dir).expanduser())
    console.print(Panel.fit(BANNER, border_style="cyan"))
    console.print(f"[dim]session: {sess.path}[/dim]")

    while True:
        try:
            line = input("\nluxe> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            return
        if not line:
            continue
        if line in ("/quit", "/exit"):
            console.print("[dim]bye[/dim]")
            return
        if line == "/help":
            console.print(HELP)
            continue
        if line == "/agents":
            for a in cfg.agents:
                mark = "x" if a.enabled else " "
                console.print(f"  \\[{mark}] {a.name:10s} {a.model:30s} {a.display}")
            continue
        if line == "/session":
            console.print(f"  {sess.path}")
            continue
        if line == "/models":
            for m in list_models():
                console.print(f"  {m}")
            continue

        def ask(q: str) -> str:
            console.print(f"[yellow]router asks:[/yellow] {q}")
            try:
                return input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                return ""

        try:
            with console.status("[dim]routing...[/dim]", spinner="dots"):
                decision = router.route(line, cfg, ask_fn=ask, session=sess)
        except KeyboardInterrupt:
            console.print("[yellow]⚠ router interrupted — returning to prompt[/yellow]")
            continue

        reasoning = f" [dim]({decision.reasoning})[/dim]" if decision.reasoning else ""
        console.print(f"[dim]→ routed to[/dim] [bold cyan]{decision.agent}[/bold cyan]{reasoning}")

        try:
            with console.status(f"[cyan]{decision.agent}[/cyan] working...", spinner="dots"):
                result = runner.dispatch(decision, cfg, session=sess)
        except KeyboardInterrupt:
            console.print("[yellow]⚠ interrupted — returning to prompt[/yellow]")
            continue

        if result.aborted:
            console.print(f"[yellow]⚠ aborted:[/yellow] {result.abort_reason}")
        if result.final_text:
            console.print(Markdown(result.final_text))
        if decision.agent == "image":
            _render_images(result.final_text)

        stats = (
            f"[dim]{decision.agent} · {result.wall_s:.1f}s · "
            f"{result.prompt_tokens}↑ {result.completion_tokens}↓ tokens · "
            f"{result.steps_taken} steps · {result.tool_calls_total} tool calls[/dim]"
        )
        console.print(stats)


def _render_images(text: str) -> None:
    """If the assistant mentioned a PNG path, render it as a clickable file:// link."""
    import re
    for m in re.finditer(r"(/\S+\.png)", text):
        p = Path(m.group(1))
        if p.exists():
            console.print(f"  [dim]↗ file://{p}[/dim]")
