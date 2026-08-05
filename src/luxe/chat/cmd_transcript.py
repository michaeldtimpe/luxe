"""What this session produced: `/diff` `/export` `/full` `/copy`.

Split out of `commands.py` 2026-08-04 (behavior unchanged).
"""

from __future__ import annotations

from luxe.chat.commands import CommandContext, CommandResult


def _diff(args, ctx: CommandContext) -> CommandResult:
    """Show what changed, scoped to THIS session's writes by default.

    `/diff`         per-file +/- for files this session wrote
    `/diff --all`   the whole working tree instead
    `/diff --full`  the patch, not just the counts
    """
    from luxe.chat import inspection
    from luxe.state import ledger as ledger_mod

    flags = {a for a in args if a.startswith("--")}
    explicit = [a for a in args if not a.startswith("--")]
    repo = ctx.session.repo_path
    if not repo:
        ctx.console.print("[yellow]No repo for this session.[/]")
        return CommandResult(handled=True)

    scope = "working tree"
    paths: list[str] | None = None
    if explicit:
        paths, scope = explicit, "selected paths"
    elif "--all" not in flags:
        led = ledger_mod.load(ctx.session.session_id) if ctx.session.session_id else None
        session_files = list(getattr(led, "files", []) or []) if led else []
        if session_files:
            paths, scope = session_files, "this session"
        else:
            # Nothing written yet — fall back to the tree rather than printing
            # an empty result that reads like "no changes anywhere".
            scope = "working tree (this session wrote nothing yet)"

    diffs, err = inspection.session_diff(repo, paths)
    if err:
        ctx.console.print(f"[red]✗ {err}[/]")
        return CommandResult(handled=True)
    if not diffs:
        ctx.console.print(f"[dim]· no changes ({scope})[/]")
        return CommandResult(handled=True)

    ctx.console.print(f"[bold]Changes[/] [dim]({scope})[/]")
    tot_a = tot_r = 0
    for d in diffs:
        if d.untracked:
            ctx.console.print(f"  [green]+[/] {d.path}  [dim](new, untracked)[/]")
            continue
        tot_a += d.added
        tot_r += d.removed
        ctx.console.print(f"  [green]+{d.added}[/] [red]-{d.removed}[/]  {d.path}")
    if tot_a or tot_r:
        ctx.console.print(f"  [dim]{len(diffs)} file(s) · "
                          f"[/][green]+{tot_a}[/] [red]-{tot_r}[/]")
    if "--full" in flags:
        patch = inspection.full_diff(repo, paths)
        if patch:
            _print_patch(ctx, patch)
    else:
        ctx.console.print("[dim]· `/diff --full` for the patch[/]")
    return CommandResult(handled=True)


def _print_patch(ctx: CommandContext, patch: str) -> None:
    """Render a unified diff with the theme's diff roles (same colours the
    verbose tool log uses), degrading to plain text if anything goes wrong."""
    from luxe.chat import theme as theme_mod

    try:
        for line in patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                ctx.console.print(theme_mod.m("diff_add", _esc(line)))
            elif line.startswith("-") and not line.startswith("---"):
                ctx.console.print(theme_mod.m("diff_del", _esc(line)))
            elif line.startswith("@@"):
                ctx.console.print(theme_mod.m("diff_hunk", _esc(line)))
            else:
                ctx.console.print(f"[dim]{_esc(line)}[/]")
    except Exception:
        ctx.console.print(patch)


def _esc(text: str) -> str:
    from rich.markup import escape
    return escape(text)


def _export(args, ctx: CommandContext) -> CommandResult:
    """Write the conversation to Markdown (default: beside the transcript)."""
    from luxe.chat import inspection

    if not ctx.session.session_id:
        ctx.console.print("[yellow]Nothing to export yet.[/]")
        return CommandResult(handled=True)
    dest = args[0] if args else None
    try:
        out = inspection.export_transcript(ctx.session.session_id, dest)
    except FileNotFoundError as e:
        ctx.console.print(f"[red]✗ {e}[/]")
        return CommandResult(handled=True)
    except OSError as e:
        ctx.console.print(f"[red]✗ cannot write export: {e}[/]")
        return CommandResult(handled=True)
    size = out.stat().st_size if out.is_file() else 0
    ctx.console.print(f"[green]✓[/] exported → {out} [dim]({size:,} bytes)[/]")
    return CommandResult(handled=True)


def _full(args, ctx: CommandContext) -> CommandResult:
    """Re-render the LAST answer with no display cap.

    The default render caps long answers (50 lines / 4000 chars) and the
    hidden tail is not reachable after the fact — `/verbose full` only
    changes FUTURE turns. The full text is always in session history, so
    this re-shows it whole without re-running anything (2026-07-30)."""
    from luxe.chat.render import build_final_renderable

    text = next((t.assistant for t in reversed(ctx.session.turns)
                 if (t.assistant or "").strip()), "")
    if not text:
        ctx.console.print("[yellow]No answer to expand yet.[/]")
        return CommandResult(handled=True)
    ctx.console.print(build_final_renderable(text, mode="full"))
    return CommandResult(handled=True)


def _copy(args, ctx: CommandContext) -> CommandResult:
    """Copy the LAST answer to the system clipboard.

    The TUI runs with mouse capture on and RichLog has no text selection, so
    terminal-level copy needs a modifier-drag most users don't know
    (Option/Shift). This is the sanctioned copy-out path alongside `/export`
    (2026-07-31). Uses the platform clipboard tool — never OSC 52 (the
    alternate-screen TUI owns the tty)."""
    import shutil
    import subprocess
    import sys

    text = next((t.assistant for t in reversed(ctx.session.turns)
                 if (t.assistant or "").strip()), "")
    if not text:
        ctx.console.print("[yellow]No answer to copy yet.[/]")
        return CommandResult(handled=True)

    candidates = ([["pbcopy"]] if sys.platform == "darwin"
                  else [["wl-copy"], ["xclip", "-selection", "clipboard"],
                        ["xsel", "--clipboard", "--input"]])
    tool = next((c for c in candidates if shutil.which(c[0])), None)
    if tool is None:
        ctx.console.print("[yellow]No clipboard tool found "
                          "(pbcopy/wl-copy/xclip/xsel). `/export` writes the "
                          "transcript to a file instead.[/]")
        return CommandResult(handled=True)
    try:
        subprocess.run(tool, input=text.encode("utf-8"), check=True, timeout=10)
    except Exception as e:
        ctx.console.print(f"[red]✗ clipboard copy failed: {e}[/]")
        return CommandResult(handled=True)
    ctx.console.print(f"[green]✓[/] copied last answer "
                      f"[dim]({len(text):,} chars)[/]")
    return CommandResult(handled=True)
