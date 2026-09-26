"""Commands that steer the next turn(s): `/goal` `/plan` `/attach` `/sys`.

Split out of `commands.py` 2026-08-04 (behavior unchanged). `ATTACH_MAX_*` are
re-exported from `commands` for back-compat with the tests that import them
there.
"""

from __future__ import annotations

import os

from rich.markup import escape

from luxe.chat.commands import CommandContext, CommandResult, _usage


def _goal(args, ctx: CommandContext) -> CommandResult:
    """Autonomous goal runner (B4): /goal <objective> starts it; /goal stop halts."""
    if not args:
        s = ctx.session
        if s.goal_active:
            ctx.console.print(f"[bold]goal active[/] [dim](round {s.goal_round}/"
                              f"{s.goal_max_rounds})[/]: {escape(s.goal)}")
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
    # A running goal owns the line REPL's prompt, so `/goal stop` can only be
    # TYPED mid-goal in the TUI; Ctrl-C (esc in the TUI) works in both.
    ctx.console.print(f"[green]✓[/] goal set [dim](starts now; Ctrl-C/esc "
                      f"halts it)[/]\n  [dim]{escape(objective)}[/]")
    return CommandResult(handled=True)


def _plan(args, ctx: CommandContext) -> CommandResult:
    """Plan mode (B5): /plan <objective> drafts a plan read-only, then the REPL
    asks whether to save it, execute it, or both."""
    objective = " ".join(args).strip()
    if not objective:
        return _usage(ctx, "/plan")
    ctx.session.plan_pending = objective
    ctx.console.print(f"[green]✓[/] planning [dim](read-only draft; you'll choose "
                      f"save / execute / both next)[/]\n  [dim]{escape(objective)}[/]")
    return CommandResult(handled=True)


# /attach caps (chat.sdd): per-file and per-turn-total ceilings on INJECTED
# characters, so a stray `/attach big.log` can't blow the context window.
ATTACH_MAX_FILE_BYTES = 48 * 1024
ATTACH_MAX_TOTAL_BYTES = 128 * 1024
_BINARY_SNIFF_BYTES = 8192


def _attach(args, ctx: CommandContext) -> CommandResult:
    """Attach file contents to the NEXT turn (one-shot).

    Reads each path now (expanduser'd; binary refused via null-byte sniff;
    48KB/file and 128KB/turn caps with an explicit truncation marker), stages
    it on `session.attachments`, and records a kind="attachment" transcript
    entry. `build_extra_context` injects the payload as `<attached_files>`
    just below `<system_constraints>` and clears the staging — one shot.
    """
    import hashlib
    from pathlib import Path

    from luxe.memory import session as session_store

    if not args:
        n = len(ctx.session.attachments)
        if n:
            ctx.console.print(f"[bold]pending attachments[/] [dim]({n}, "
                              f"injected into the next turn)[/]")
            for a in ctx.session.attachments:
                mark = " [yellow](truncated)[/]" if a.get("truncated") else ""
                ctx.console.print(f"  [cyan]{escape(a['path'])}[/] "
                                  f"[dim]{a['size']} bytes[/]{mark}")
        else:
            _usage(ctx, "/attach")
        return CommandResult(handled=True)

    total = sum(len(a["content"]) for a in ctx.session.attachments)
    for raw in args:
        p = Path(os.path.expanduser(raw))
        if not p.is_file():
            ctx.console.print(f"[yellow]✗ {escape(raw)}: no such file[/]")
            continue
        try:
            data = p.read_bytes()
        except OSError as e:
            ctx.console.print(f"[yellow]✗ {escape(raw)}: {escape(str(e))}[/]")
            continue
        if b"\0" in data[:_BINARY_SNIFF_BYTES]:
            ctx.console.print(f"[yellow]✗ {escape(raw)}: looks binary — refused[/] "
                              "[dim](ask the model to read it instead — it "
                              "has read_file/bash)[/]")
            continue
        text = data.decode("utf-8", errors="replace")
        truncated = False
        if len(text) > ATTACH_MAX_FILE_BYTES:
            text = (text[:ATTACH_MAX_FILE_BYTES]
                    + f"\n…[luxe: truncated — first {ATTACH_MAX_FILE_BYTES} of "
                      f"{len(data)} bytes shown]")
            truncated = True
        if total + len(text) > ATTACH_MAX_TOTAL_BYTES:
            ctx.console.print(
                f"[yellow]✗ {escape(raw)}: skipped — {ATTACH_MAX_TOTAL_BYTES // 1024}KB "
                f"total attachment cap reached[/] [dim](attachments are "
                "one-shot: send this turn, then `/attach` it on the next — or "
                "just name the path and let the model read it)[/]")
            continue
        total += len(text)
        att = {
            "path": str(p),
            "content": text,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "truncated": truncated,
        }
        ctx.session.attachments.append(att)
        # Transcript provenance (resume._pair_turns ignores unknown kinds).
        if ctx.session.session_id:
            session_store.append_turn(
                ctx.session.session_id, "attachment",
                path=att["path"], size=att["size"], sha256=att["sha256"],
                truncated=truncated, content=text,
            )
        mark = (f" [yellow](truncated to {ATTACH_MAX_FILE_BYTES // 1024}KB)[/]"
                if truncated else "")
        ctx.console.print(f"[green]✓[/] attached [cyan]{escape(str(p))}[/] "
                          f"[dim]({att['size']} bytes)[/]{mark}")
    if ctx.session.attachments:
        ctx.console.print("[dim]· injected into the NEXT turn only (one-shot)[/]")
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
                ctx.console.print(f"  [cyan]{i}[/] {escape(c)}")
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
            ctx.console.print(f"[green]✓[/] removed constraint [cyan]{idx}[/]: "
                              f"{escape(removed)}")
        except (ValueError, IndexError):
            ctx.console.print(f"[yellow]No constraint at index {escape(repr(args[1]))}. "
                              f"Use /sys list to see indices.[/]")
        return CommandResult(handled=True)

    if sub == "clear":
        count = len(ctx.session.system_constraints)
        ctx.session.system_constraints.clear()
        ctx.console.print(f"[green]✓[/] cleared {count} constraint(s)")
        return CommandResult(handled=True)

    ctx.console.print(f"[yellow]Unknown /sys subcommand {escape(repr(sub))}. "
                      f"Expected: add <rule> | list | remove <index> | clear[/]")
    return CommandResult(handled=True)
