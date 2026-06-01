"""Slash-command parsing + dispatch for the chat REPL.

Commands are decoupled from the loop via a `CommandContext` carrying the
session, the slot manager, the console, and injected hooks for the heavier
features (compare, resume) that the REPL wires in.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable

from rich.console import Console

from luxe.chat.session import CTX_TIERS, ChatSession, tier_label
from luxe.chat.slots import SlotManager
from luxe.memory import project as project_mem

_SLOTS = ("chat", "plan", "code")


@dataclass
class CommandResult:
    handled: bool
    exit: bool = False


@dataclass
class CommandContext:
    console: Console
    session: ChatSession
    slots: SlotManager
    on_compare: Callable[[str], None] | None = None
    on_compare_review: Callable[[str], None] | None = None
    on_resume: Callable[[str], None] | None = None


_HELP = """[bold]luxe chat commands[/]
  [cyan]/help[/]                      show this help
  [cyan]/model[/] [slot] [model_id]   show slots, or repoint chat|plan|code
  [cyan]/use[/] <slot>                pin the next turn to chat|plan|code
  [cyan]/ctx[/] [small|medium|large|xlarge]   show or set context window size
  [cyan]/write[/]                     toggle write tools (default: read-only)
  [cyan]/memory[/] list|add|promote|forget|edit
  [cyan]/compare[/] <task>            run two configs side-by-side
  [cyan]/compare review[/] [id]       replay a stored comparison
  [cyan]/resume[/] [id]               resume a prior session (or list them)
  [cyan]/clear[/]                     start a fresh conversation
  [cyan]/quit[/]                      exit (Ctrl-D also works)
"""


def is_command(line: str) -> bool:
    return line.strip().startswith("/")


def dispatch(line: str, ctx: CommandContext) -> CommandResult:
    parts = line.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]
    handlers = {
        "/help": _help,
        "/model": _model,
        "/use": _use,
        "/ctx": _ctx,
        "/write": _write,
        "/memory": _memory,
        "/compare": _compare,
        "/resume": _resume,
        "/clear": _clear,
        "/quit": _quit,
        "/exit": _quit,
    }
    fn = handlers.get(cmd)
    if fn is None:
        ctx.console.print(f"[yellow]Unknown command {cmd}. Try /help.[/]")
        return CommandResult(handled=True)
    return fn(args, ctx)


def _help(args, ctx: CommandContext) -> CommandResult:
    ctx.console.print(_HELP)
    return CommandResult(handled=True)


def _model(args, ctx: CommandContext) -> CommandResult:
    if not args:
        ctx.console.print("[bold]Slots[/] (resident: "
                          f"[cyan]{ctx.slots.resident}[/])")
        for slot, model in ctx.slots.slot_models().items():
            ctx.console.print(f"  {slot:5s} → {model}")
        return CommandResult(handled=True)
    slot = args[0]
    if slot not in _SLOTS:
        ctx.console.print(f"[yellow]Unknown slot {slot!r}; expected chat|plan|code.[/]")
        return CommandResult(handled=True)
    if len(args) < 2:
        ctx.console.print(f"  {slot} → {ctx.slots.model_for(slot)}")
        return CommandResult(handled=True)
    model_id = args[1]
    ctx.slots.set_override(slot, model_id)
    ctx.console.print(f"[green]✓[/] slot [cyan]{slot}[/] → {model_id} "
                      f"[dim](swaps on next {slot} turn)[/]")
    return CommandResult(handled=True)


def _use(args, ctx: CommandContext) -> CommandResult:
    if not args or args[0] not in _SLOTS:
        ctx.console.print("[yellow]Usage: /use chat|plan|code[/]")
        return CommandResult(handled=True)
    ctx.session.pinned_slot = args[0]
    ctx.console.print(f"[green]✓[/] next turn pinned to slot [cyan]{args[0]}[/]")
    return CommandResult(handled=True)


def _ctx(args, ctx: CommandContext) -> CommandResult:
    # Display against the conversational `chat` slot (the default route).
    ceiling = ctx.slots.ctx_ceiling("chat")
    base = ctx.slots.role_for("chat").num_ctx
    active = ctx.session.num_ctx_override or base

    def _tiers_line() -> str:
        bits = []
        for name, n in CTX_TIERS.items():
            mark = "[dim](>max)[/]" if n > ceiling else ""
            bits.append(f"{name} [dim]{n}[/]{mark}")
        return "  ".join(bits)

    if not args:
        eff = min(active, ceiling)
        clamp = f" [dim](clamped from {active})[/]" if eff != active else ""
        ctx.console.print(
            f"context window: [cyan]{tier_label(eff)}[/] [dim]num_ctx {eff}[/]{clamp}"
            f"  [dim]· max {ceiling}[/]")
        ctx.console.print(f"[dim]tiers:[/] {_tiers_line()}")
        ctx.console.print("[dim]Bigger windows hold more code but cost KV-cache "
                          "RAM and tokens. Set with /ctx <tier>.[/]")
        return CommandResult(handled=True)

    tier = args[0].lower()
    if tier not in CTX_TIERS:
        ctx.console.print(f"[yellow]Unknown size {tier!r}; expected "
                          f"{'|'.join(CTX_TIERS)}.[/]")
        return CommandResult(handled=True)

    requested = CTX_TIERS[tier]
    ctx.session.num_ctx_override = requested
    eff = min(requested, ceiling)
    if eff != requested:
        ctx.console.print(
            f"[yellow]✓[/] context → [cyan]{tier}[/] requested ({requested}), "
            f"[yellow]clamped to {eff}[/] [dim](this box's max; raise num_ctx_max "
            f"in the config to go higher)[/]")
    else:
        ctx.console.print(f"[green]✓[/] context → [cyan]{tier}[/] "
                          f"[dim](num_ctx {eff}; applies next turn)[/]")
    return CommandResult(handled=True)


def _write(args, ctx: CommandContext) -> CommandResult:
    ctx.session.write_enabled = not ctx.session.write_enabled
    if ctx.session.write_enabled:
        ctx.console.print("write tools: [yellow]ON[/] "
                          "[dim](write_file, edit_file, bash enabled — /write to disable)[/]")
    else:
        ctx.console.print("write tools: [green]OFF[/] "
                          "[dim](read-only; /write to enable file creation/edits)[/]")
    return CommandResult(handled=True)


def _memory(args, ctx: CommandContext) -> CommandResult:
    repo = ctx.session.repo_path
    if not repo:
        ctx.console.print("[yellow]No repo bound to this session.[/]")
        return CommandResult(handled=True)
    sub = args[0] if args else "list"
    if sub == "list":
        mem = project_mem.load_memory(repo)
        if mem.curated_md.strip():
            ctx.console.print("[bold]curated (.luxe/memory.md)[/]")
            ctx.console.print(f"[dim]{mem.curated_md.strip()}[/]")
        if mem.facts:
            ctx.console.print("[bold]facts[/]")
            for f in mem.facts:
                tag = "[green]✓[/]" if f.confidence == "manual" else "[dim]·[/]"
                ctx.console.print(f"  {tag} [cyan]{f.id}[/] ({f.kind}) {f.text} "
                                  f"[dim]{f.confidence}[/]")
        if not mem.curated_md.strip() and not mem.facts:
            ctx.console.print("[dim](no project memory yet)[/]")
    elif sub == "add":
        text = " ".join(args[1:]).strip()
        if not text:
            ctx.console.print("[yellow]Usage: /memory add <text>[/]")
            return CommandResult(handled=True)
        # User-added memory is curated → injected immediately.
        f = project_mem.add_fact(repo, text, source="user", confidence="manual")
        ctx.console.print(f"[green]✓[/] saved [cyan]{f.id}[/] (injected)")
    elif sub == "promote":
        if len(args) < 2:
            ctx.console.print("[yellow]Usage: /memory promote <id>[/]")
            return CommandResult(handled=True)
        ok = project_mem.promote_fact(repo, args[1])
        ctx.console.print("[green]✓ promoted[/]" if ok else "[yellow]no such fact[/]")
    elif sub == "forget":
        if len(args) < 2:
            ctx.console.print("[yellow]Usage: /memory forget <id>[/]")
            return CommandResult(handled=True)
        ok = project_mem.forget_fact(repo, args[1])
        ctx.console.print("[green]✓ forgotten[/]" if ok else "[yellow]no such fact[/]")
    elif sub == "edit":
        path = project_mem.repo_memory_file(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        editor = os.environ.get("EDITOR", "vi")
        subprocess.call([editor, str(path)])
    else:
        ctx.console.print(f"[yellow]Unknown /memory subcommand {sub!r}.[/]")
    return CommandResult(handled=True)


def _compare(args, ctx: CommandContext) -> CommandResult:
    if args and args[0] == "review":
        if ctx.on_compare_review is None:
            ctx.console.print("[yellow]compare review unavailable.[/]")
        else:
            ctx.on_compare_review(args[1] if len(args) > 1 else "")
        return CommandResult(handled=True)
    task = " ".join(args).strip()
    if not task:
        ctx.console.print("[yellow]Usage: /compare <task>[/]")
        return CommandResult(handled=True)
    if ctx.on_compare is None:
        ctx.console.print("[yellow]compare unavailable.[/]")
    else:
        ctx.on_compare(task)
    return CommandResult(handled=True)


def _resume(args, ctx: CommandContext) -> CommandResult:
    if ctx.on_resume is None:
        ctx.console.print("[yellow]resume unavailable.[/]")
        return CommandResult(handled=True)
    ctx.on_resume(args[0] if args else "")
    return CommandResult(handled=True)


def _clear(args, ctx: CommandContext) -> CommandResult:
    ctx.session.turns.clear()
    ctx.session.pinned_slot = None
    ctx.console.print("[dim]· conversation cleared[/]")
    return CommandResult(handled=True)


def _quit(args, ctx: CommandContext) -> CommandResult:
    return CommandResult(handled=True, exit=True)
