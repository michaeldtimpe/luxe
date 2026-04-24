"""Aliases, pins, memory/editor, history/sessions printing, image rendering."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from rich.console import Console

from luxe import prefs
from luxe.registry import LuxeConfig
from luxe.repl.help import BUILTIN_CMDS
from luxe.session import Session, list_sessions

console = Console()


BUILTIN_CMDS_NAMES = {c.lstrip("/") for c in BUILTIN_CMDS} | {
    "general", "research", "writing", "image", "code",
}


def _apply_pins(prompt: str, pins: list[str]) -> str:
    if not pins:
        return prompt
    block = "\n".join(f"- {p}" for p in pins)
    return f"[Pinned context]\n{block}\n\n{prompt}"


def _expand_alias(line: str) -> str:
    if not line.startswith("/"):
        return line
    head, _, rest = line[1:].partition(" ")
    if head in BUILTIN_CMDS_NAMES:
        return line
    aliases = prefs.load_aliases()
    if head not in aliases:
        return line
    expansion = aliases[head]
    return f"{expansion} {rest}".strip() if rest else expansion


def _handle_alias(args: list[str]) -> None:
    if not args:
        console.print("[yellow]usage:[/yellow] /alias add|list|remove ...")
        return
    sub = args[0]
    if sub == "list":
        aliases = prefs.load_aliases()
        if not aliases:
            console.print("[dim]no aliases[/dim]")
            return
        width = max(len(k) for k in aliases)
        for k, v in sorted(aliases.items()):
            console.print(f"  [cyan]/{k:<{width}}[/cyan]  {v}")
        return
    if sub == "add":
        if len(args) < 3:
            console.print("[yellow]usage:[/yellow] /alias add <name> <expansion>")
            return
        name = args[1]
        if name in BUILTIN_CMDS_NAMES:
            console.print(f"[yellow]cannot shadow builtin:[/yellow] /{name}")
            return
        expansion = " ".join(args[2:])
        prefs.save_alias(name, expansion)
        console.print(f"[dim]alias /{name} →[/dim] {expansion}")
        return
    if sub == "remove":
        if len(args) < 2:
            console.print("[yellow]usage:[/yellow] /alias remove <name>")
            return
        if prefs.remove_alias(args[1]):
            console.print(f"[dim]removed alias[/dim] /{args[1]}")
        else:
            console.print(f"[yellow]no such alias:[/yellow] /{args[1]}")
        return
    console.print(f"[yellow]unknown alias subcommand:[/yellow] {sub}")


def _edit_memory() -> None:
    prefs._ensure_dir()
    if not prefs.MEMORY_FILE.exists():
        prefs.MEMORY_FILE.write_text(
            "# luxe memory\n\nGuidance injected into every specialist's system prompt.\n"
        )
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    try:
        subprocess.run([editor, str(prefs.MEMORY_FILE)], check=False)
    except FileNotFoundError:
        console.print(f"[yellow]$EDITOR not found:[/yellow] {editor}")
        return
    size = prefs.MEMORY_FILE.stat().st_size
    console.print(f"[dim]memory: {size} bytes at {prefs.MEMORY_FILE}[/dim]")


def _print_history(sess: Session, n: int) -> None:
    events = sess.read_all()
    if not events:
        console.print("[dim]no history yet[/dim]")
        return
    for e in events[-n:]:
        role = e.get("role", "?")
        agent = e.get("agent", "")
        text = str(e.get("content") or e.get("tool") or "")
        if len(text) > 200:
            text = text[:200] + "…"
        console.print(f"  [dim]{role}/{agent}:[/dim] {text}")


def _print_last_events(sess: Session, n: int) -> None:
    events = sess.read_all()
    if not events:
        return
    console.print(f"[dim]last {min(n, len(events))} events:[/dim]")
    for e in events[-n:]:
        role = e.get("role", "?")
        agent = e.get("agent", "")
        text = str(e.get("content") or e.get("tool") or "")
        if len(text) > 110:
            text = text[:110] + "…"
        console.print(f"  [dim]{role}/{agent}:[/dim] {text}")


def _print_sessions(cfg: LuxeConfig) -> None:
    root = Path(cfg.session_dir).expanduser()
    sessions = list_sessions(root)
    bookmarks = prefs.load_bookmarks()
    inv = {v: k for k, v in bookmarks.items()}

    if bookmarks:
        console.print("[bold]Bookmarks[/bold]")
        for name, sid in sorted(bookmarks.items()):
            p = root / f"{sid}.jsonl"
            mark = " " if p.exists() else "!"
            console.print(f"  [cyan]{name:<20s}[/cyan] {sid} {mark}")

    if not sessions:
        if not bookmarks:
            console.print("[yellow]no sessions yet[/yellow]")
        return
    console.print("[bold]Recent sessions[/bold]")
    for p in sessions[:15]:
        sid = p.stem
        label = inv.get(sid, "")
        suffix = f"   [dim]({label})[/dim]" if label else ""
        console.print(f"  {sid}{suffix}")


def _render_images(text: str) -> None:
    """If the assistant mentioned a PNG path, render it as a clickable file:// link."""
    import re
    for m in re.finditer(r"(/\S+\.png)", text):
        p = Path(m.group(1))
        if p.exists():
            console.print(f"  [dim]↗ file://{p}[/dim]")
