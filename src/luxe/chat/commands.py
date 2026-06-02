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
  [cyan]/bash[/]                      toggle unrestricted shell (default: allowlisted)
  [cyan]/verbose[/] [diff|full|off]   show full tool I/O (diffs, file contents, results)
  [cyan]/reasoning[/]                 toggle live streaming of the model's thinking
  [cyan]/goal[/] <objective> | stop   autonomously run rounds until the objective is met
  [cyan]/sys[/] [add <rule>|list|clear]  manage session-scoped system constraints
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
        "/bash": _bash_mode,
        "/verbose": _verbose,
        "/reasoning": _reasoning,
        "/goal": _goal,
        "/sys": _sys,
        "/memory": _memory,
        "/compare": _compare,
        "/resume": _resume,
        "/clear": _clear,
        "/quit": _quit,
        "/exit": _quit,   # hidden alias (not listed in /help)
        "/q": _quit,      # hidden quick-exit alias
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


def _bash_mode(args, ctx: CommandContext) -> CommandResult:
    ctx.session.unrestricted_bash = not ctx.session.unrestricted_bash
    if ctx.session.unrestricted_bash:
        ctx.console.print(
            "shell: [red]UNRESTRICTED[/] [dim](any command — chains, pipes, "
            "redirects, venv/pip/build/test; cwd=repo root, NOT sandboxed)[/]")
        if not ctx.session.write_enabled:
            ctx.console.print("[yellow]· note: bash is only exposed in write mode — "
                              "run /write to enable it[/]")
    else:
        ctx.console.print("shell: [green]allowlisted[/] "
                          "[dim](safe binaries only; /bash for unrestricted dev mode)[/]")
    return CommandResult(handled=True)


_VERBOSE_LEVELS = ("off", "diff", "full")


def _verbose(args, ctx: CommandContext) -> CommandResult:
    """Tool-I/O visibility (B2): off | diff | full. Bare /verbose toggles
    off<->diff. Independent of /reasoning."""
    cur = ctx.session.verbose_level
    if args:
        lvl = args[0].lower()
        if lvl not in _VERBOSE_LEVELS:
            ctx.console.print(f"[yellow]Usage: /verbose [diff|full|off] "
                              f"(current: {cur})[/]")
            return CommandResult(handled=True)
    else:
        lvl = "diff" if cur == "off" else "off"
    ctx.session.verbose_level = lvl
    if lvl == "off":
        ctx.console.print("verbose: [green]OFF[/] [dim](one-line tool summaries)[/]")
    elif lvl == "diff":
        ctx.console.print("verbose: [yellow]DIFF[/] [dim](edits as diffs, write "
                          "headers, result bodies, ledger view)[/]")
    else:
        ctx.console.print("verbose: [red]FULL[/] [dim](entire file contents + full "
                          "result bodies — can be very long)[/]")
    return CommandResult(handled=True)


def _reasoning(args, ctx: CommandContext) -> CommandResult:
    """Toggle live streaming of the model's thinking (B2), independent of /verbose."""
    ctx.session.show_reasoning = not ctx.session.show_reasoning
    if ctx.session.show_reasoning:
        ctx.console.print("reasoning: [yellow]ON[/] [dim](streams model prose live; "
                          "responsiveness tracks the backend's streaming cadence)[/]")
    else:
        ctx.console.print("reasoning: [green]OFF[/] [dim](hidden)[/]")
    return CommandResult(handled=True)


def _goal(args, ctx: CommandContext) -> CommandResult:
    """Autonomous goal runner (B4): /goal <objective> starts it; /goal stop halts."""
    if not args:
        s = ctx.session
        if s.goal_active:
            ctx.console.print(f"[bold]goal active[/] [dim](round {s.goal_round}/"
                              f"{s.goal_max_rounds})[/]: {s.goal}")
        else:
            ctx.console.print("[yellow]Usage: /goal <objective>  ·  /goal stop[/]")
        return CommandResult(handled=True)
    if args[0].lower() == "stop":
        if ctx.session.goal_active:
            ctx.session.goal_active = False
            ctx.console.print("[yellow]· goal stopped.[/]")
        else:
            ctx.console.print("[dim]· no active goal.[/]")
        return CommandResult(handled=True)
    if not ctx.session.write_enabled:
        ctx.console.print("[yellow]· goal mode needs write tools — run /write first "
                          "(and /bash if the task builds/tests).[/]")
        return CommandResult(handled=True)
    objective = " ".join(args).strip()
    ctx.session.goal = objective
    ctx.session.goal_active = True
    ctx.session.goal_round = 0
    ctx.session.consecutive_crashes = 0
    ctx.console.print(f"[green]✓[/] goal set [dim](starts now; /goal stop or Ctrl-C "
                      f"to halt)[/]\n  [dim]{objective}[/]")
    return CommandResult(handled=True)


def _sys(args, ctx: CommandContext) -> CommandResult:
    """Manage session-scoped system constraints injected into every turn's context."""
    sub = args[0].lower() if args else "list"

    if sub == "list":
        constraints = ctx.session.system_constraints
        if not constraints:
            ctx.console.print("[dim](no system constraints set — use /sys add <rule>)[/]")
        else:
            ctx.console.print(f"[bold]system constraints[/] [dim]({len(constraints)} active)[/]")
            for i, c in enumerate(constraints):
                ctx.console.print(f"  [cyan]{i}[/] {c}")
        return CommandResult(handled=True)

    if sub == "add":
        rule = " ".join(args[1:]).strip()
        if not rule:
            ctx.console.print("[yellow]Usage: /sys add <rule>[/]")
            return CommandResult(handled=True)
        ctx.session.system_constraints.append(rule)
        idx = len(ctx.session.system_constraints) - 1
        ctx.console.print(f"[green]✓[/] constraint [cyan]{idx}[/] added "
                          f"[dim](injected into every subsequent turn)[/]")
        return CommandResult(handled=True)

    if sub == "remove":
        if len(args) < 2:
            ctx.console.print("[yellow]Usage: /sys remove <index>[/]")
            return CommandResult(handled=True)
        try:
            idx = int(args[1])
            removed = ctx.session.system_constraints.pop(idx)
            ctx.console.print(f"[green]✓[/] removed constraint [cyan]{idx}[/]: {removed}")
        except (ValueError, IndexError):
            ctx.console.print(f"[yellow]No constraint at index {args[1]!r}. "
                              f"Use /sys list to see indices.[/]")
        return CommandResult(handled=True)

    if sub == "clear":
        count = len(ctx.session.system_constraints)
        ctx.session.system_constraints.clear()
        ctx.console.print(f"[green]✓[/] cleared {count} constraint(s)")
        return CommandResult(handled=True)

    ctx.console.print(f"[yellow]Unknown /sys subcommand {sub!r}. "
                      f"Expected: add <rule> | list | remove <index> | clear[/]")
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
