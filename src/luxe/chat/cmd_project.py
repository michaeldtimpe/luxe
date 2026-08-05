"""Commands about the repo this session is attached to.

`/project` `/index` `/init` `/note` `/memory` `/gitaudit` `/gitchange` `/compare`.
Split out of `commands.py` 2026-08-04 (behavior unchanged).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from luxe.chat.commands import CommandContext, CommandResult, _usage
from luxe.memory import project as project_mem


def _project(args, ctx: CommandContext) -> CommandResult:
    """Show, attach, or switch the project this session is about.

    `/project`          what's attached now (and whether it's indexed)
    `/project <path>`   attach/switch: re-resolve, re-index, move the repo lock

    A session started outside a project is a normal way to use luxe — this is
    how you give it a codebase without restarting.
    """
    from luxe import project as project_mod
    from luxe.chat import repl as repl_mod

    if not args:
        kind = ctx.session.project_kind
        root = ctx.session.repo_path or "(none)"
        label = {"git": "git repo", "dir": "project", "none": "no project"}.get(
            kind, kind)
        ctx.console.print(f"[bold]Project[/] {root}  [dim]({label})[/]")
        avail = repl_mod.index_tools_available()
        for tool, ok in avail.items():
            mark = "[green]✓[/]" if ok else "[yellow]·[/]"
            ctx.console.print(f"  {mark} {tool}"
                              + ("" if ok else "  [dim](no index)[/]"))
        if not all(avail.values()):
            ctx.console.print("[dim]· `/index` to build it here, or "
                              "`/project <path>` to attach a codebase[/]")
        return CommandResult(handled=True)

    if ctx.on_project is None:
        ctx.console.print("[yellow]This session can't switch projects.[/]")
        return CommandResult(handled=True)

    target = str(Path(args[0]).expanduser())
    if not Path(target).is_dir():
        ctx.console.print(f"[red]✗ not a directory: {args[0]}[/]")
        return CommandResult(handled=True)
    resolved = project_mod.resolve(target)
    if resolved.root != str(Path(target).resolve()):
        ctx.console.print(f"[dim]· {args[0]} is inside {resolved.root} "
                          "— attaching the whole project[/]")
    return _do_attach(ctx, target, verb="project")


def _index_cmd(args, ctx: CommandContext) -> CommandResult:
    """Build the code-search index for the current directory (or `<path>`).

    The escape hatch for "I started outside a project but I do want search
    here", and for re-indexing after the tree moved.
    """
    if ctx.on_project is None:
        ctx.console.print("[yellow]This session can't build an index.[/]")
        return CommandResult(handled=True)
    target = str(Path(args[0]).expanduser()) if args else None
    if target and not Path(target).is_dir():
        ctx.console.print(f"[red]✗ not a directory: {args[0]}[/]")
        return CommandResult(handled=True)
    return _do_attach(ctx, target, verb="index")


def _do_attach(ctx: CommandContext, target: str | None, *, verb: str) -> CommandResult:
    """Shared body: run the cli-provided attach hook and report the outcome."""
    from luxe.locks import LockHeld

    try:
        summary = ctx.on_project(target)
    except LockHeld as e:
        ctx.console.print(f"[red]✗ {e}[/] [dim](staying on "
                          f"{ctx.session.repo_path})[/]")
        return CommandResult(handled=True)
    except Exception as e:
        ctx.console.print(f"[red]✗ {verb} failed: {type(e).__name__}: {e}[/]")
        return CommandResult(handled=True)

    root, kind = summary["root"], summary["kind"]
    ctx.session.repo_path = root
    ctx.session.project_kind = kind
    if kind == "none":
        ctx.console.print(
            f"[yellow]· {root} isn't a project[/] [dim]— no index built. Read "
            "tools still work; pass a path with a repo or a project marker "
            "(pyproject.toml, package.json, …).[/]")
        return CommandResult(handled=True)
    how = "git-tracked" if summary.get("used_git") else "walked"
    ctx.console.print(
        f"[green]✓[/] project → {root} [dim]({summary['label']})[/]\n"
        f"  [dim]indexed {summary['files']} files · {summary['symbols']} "
        f"symbols · {how}[/]")
    if summary.get("truncated"):
        ctx.console.print(f"[yellow]· index truncated at the "
                          f"{summary['truncated']}[/]")
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
        return _usage(ctx, "/compare")
    if ctx.on_compare is None:
        ctx.console.print("[yellow]compare unavailable.[/]")
    else:
        ctx.on_compare(task)
    return CommandResult(handled=True)


def _parse_deep(args) -> bool | None:
    """Map a `/gitaudit deep|shallow` arg to deep override (None = auto). Accepts
    the dispatcher's token list or a raw string."""
    if isinstance(args, str):
        tokens = args.lower().split()
    else:
        tokens = [str(a).lower() for a in (args or [])]
    if "deep" in tokens:
        return True
    if "shallow" in tokens or "no-deep" in tokens:
        return False
    return None


def _git_analysis(kind: str, ctx: CommandContext,
                  deep: bool | None = None) -> CommandResult:
    """Run a read-only gitkit report on the SESSION repo (CLI targets other
    repos). Delegates to the injected hook so the heavy run_single call lives in
    the REPL, not here. `deep` (None=auto, True=deep, False=single-pass) forces
    the staged deep-mode dispatch."""
    if not ctx.session.repo_path:
        ctx.console.print("[yellow]No repo bound to this session. Use the CLI "
                          f"(`luxe {kind} <path-or-url>`) to analyze another repo.[/]")
        return CommandResult(handled=True)
    if ctx.on_git_analysis is None:
        ctx.console.print("[yellow]git analysis unavailable.[/]")
        return CommandResult(handled=True)
    ctx.on_git_analysis(kind, deep)
    return CommandResult(handled=True)


def _init(args, ctx: CommandContext) -> CommandResult:
    """Draft this repo's orientation brief into `.luxe/memory.md`.

    Same engine as `luxe init` (gitkit/brief.py) against the SESSION's repo
    and endpoint. Read-only by construction — the one file it writes is
    luxe's own state file, and only the fenced `luxe:brief` block inside it
    (see chat.sdd)."""
    from luxe.gitkit import brief as brief_mod

    if not ctx.session.repo_path or ctx.session.project_kind == "none":
        ctx.console.print("[yellow]No project bound to this session — "
                          "`/project <path>` first, or run `luxe init <path>` "
                          "from the CLI.[/]")
        return CommandResult(handled=True)
    dry = "--dry-run" in args
    try:
        backend = ctx.slots.backend_for("chat")
    except Exception:
        backend = None
    result = brief_mod.run_init(ctx.session.repo_path, ctx.slots.cfg,
                                console=ctx.console, dry_run=dry,
                                backend=backend)
    if not result.ok:
        ctx.console.print(f"[red]✗ {result.error}[/]")
        return CommandResult(handled=True)
    if dry:
        from rich.markdown import Markdown
        ctx.console.print(Markdown(result.text))
        ctx.console.print("[dim]· --dry-run: nothing written[/]")
        return CommandResult(handled=True)
    ctx.console.print(f"[green]✓[/] brief → {result.written} "
                      f"[dim]({len(result.text)} chars"
                      f"{', truncated' if result.truncated else ''}; injected "
                      "as <project_memory> from the next turn)[/]")
    return CommandResult(handled=True)


def _note(args, ctx: CommandContext) -> CommandResult:
    """Distil this session into working notes in `.luxe/memory.md` now.

    The same distillation that runs on `/quit`, on demand — so a long session
    can bank what it learned before it ends. Explicit invocation is consent:
    it ignores the `notes:` config toggle and the turn-count floor."""
    from luxe.chat import notes as notes_mod

    res = notes_mod.run_session_notes(ctx.session, ctx.slots, ctx.slots.cfg,
                                      ctx.console, on_demand=True)
    if res.written is None and res.skipped and "no project" not in res.skipped:
        ctx.console.print(f"[yellow]· no session notes written ({res.skipped})[/]")
    return CommandResult(handled=True)


def _gitaudit(args, ctx: CommandContext) -> CommandResult:
    return _git_analysis("gitaudit", ctx, _parse_deep(args))


def _gitchange(args, ctx: CommandContext) -> CommandResult:
    return _git_analysis("gitchange", ctx, _parse_deep(args))
