"""Session switches: `/theme` `/use` `/ctx` and the on/off toggles.

`/write` `/bash` `/web` `/verbose` `/reasoning` `/debug` `/terse` `/compact`.
Split out of `commands.py` 2026-08-04 (behavior unchanged); the five handlers
that were the same eight lines with different words now come from `_toggle`,
which reproduces their output byte for byte.
"""

from __future__ import annotations

from typing import Callable

from luxe.chat.commands import _SLOTS, CommandContext, CommandResult
from luxe.chat.session import CTX_TIERS, tier_label


def _theme(args, ctx: CommandContext) -> CommandResult:
    """Show, preview, or switch the colour palette without restarting.

    `/theme`          list palettes, marking the active one
    `/theme preview`  render THIS session's status bar in every palette
    `/theme <name>`   switch (auto = track your terminal/statusline theme)
    """
    from luxe.chat import theme as theme_mod

    choices = theme_mod.list_palettes()
    if args and args[0].lower() in ("preview", "demo", "sample"):
        _theme_preview(ctx, choices)
        return CommandResult(handled=True)
    if not args:
        current = theme_mod.active_palette()
        saved = theme_mod.load_preference()
        ctx.console.print("[bold]Palettes[/]")
        for name in choices:
            mark = " [cyan]← active[/]" if name == current else ""
            mark += " [dim](saved default)[/]" if name == saved else ""
            hint = ("  [dim](tracks your terminal/statusline theme)[/]"
                    if name == "auto" else "")
            ctx.console.print(f"  {name}{hint}{mark}")
        ctx.console.print("[dim]compare with /theme preview · "
                          "switch with /theme <name> (sticks across "
                          "sessions)[/]")
        return CommandResult(handled=True)

    name = args[0].lower()
    if name not in choices:
        ctx.console.print(f"[yellow]Unknown palette {name!r}. "
                          f"Choose from: {', '.join(choices)}.[/]")
        return CommandResult(handled=True)
    theme_mod.set_palette(name)
    saved = theme_mod.save_preference(name)
    ctx.console.print(f"[green]✓[/] palette → [cyan]{name}[/]"
                      + ("  [dim](tracking your terminal theme)[/]"
                         if name == "auto" else "")
                      + ("  [dim]· saved — future sessions start here[/]"
                         if saved else ""))
    _theme_sample(ctx, label="")
    return CommandResult(handled=True)


def _theme_preview(ctx: CommandContext, choices: list[str]) -> None:
    """Render the real status bar (and a tool + diff line) once per palette.

    Comparing palettes by name is guesswork; this shows the actual thing being
    coloured, with the session's own data, so a choice takes one look. The active
    palette is restored afterwards — previewing must not change anything.
    """
    from luxe.chat import theme as theme_mod

    before = theme_mod.active_palette()
    try:
        for name in choices:
            theme_mod.set_palette(name)
            mark = " [cyan]← active[/]" if name == before else ""
            ctx.console.print(f"\n[bold]{name}[/]{mark}")
            _theme_sample(ctx, label=name)
    finally:
        theme_mod.set_palette(before)
    ctx.console.print(f"\n[dim]· switch with `/theme <name>`; still on "
                      f"[cyan]{before}[/][/]")


def _theme_sample(ctx: CommandContext, *, label: str) -> None:
    """One palette's worth of sample output: status bar + tool + diff lines."""
    from luxe.chat import status as status_mod
    from luxe.chat import theme as theme_mod

    try:
        segs = status_mod.fields(ctx.session, ctx.slots,
                                ctx.session.repo_path or "", ctx.status
                                or status_mod.StatusState())
        ctx.console.print(status_mod.to_rich_text(status_mod.fit(segs, 96)))
    except Exception:
        pass
    ctx.console.print("  " + theme_mod.m("accent", "read_file")
                      + " " + theme_mod.m("muted", "src/luxe/cli.py")
                      + "  " + theme_mod.m("success", "✓ 1.2k")
                      + "   " + theme_mod.m("error", "✗ error")
                      + "   " + theme_mod.m("warn", "! warning")
                      + "   " + theme_mod.m("info", "· note"))
    ctx.console.print("  " + theme_mod.m("diff_hunk", "@@ -1,3 +1,4 @@")
                      + " " + theme_mod.m("diff_add", "+added")
                      + " " + theme_mod.m("diff_del", "-removed"))


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


# Five commands were the same eight lines with different words: flip one bool
# on the session, print `<name>: [<style>]<LABEL>[/] [dim](<hint>)[/]`. The
# factory below reproduces that line BYTE FOR BYTE — the styles are per-command
# on purpose (`/terse` is green when ON because terse is the safe default,
# `/write` is yellow because it isn't), so they are parameters, not a
# convention. `after` covers /bash's conditional second line.
#
# /web and /debug stay hand-written: /web prints a per-provider report and
# /debug drives two fields off a compound predicate. Neither is this shape.
def _toggle(
    attr: str,
    name: str,
    *,
    on_label: str = "ON",
    off_label: str = "OFF",
    on_style: str = "yellow",
    off_style: str = "green",
    on_hint: str = "",
    off_hint: str = "",
    after: "Callable[[CommandContext, bool], None] | None" = None,
) -> "Callable[[list, CommandContext], CommandResult]":
    def handler(args, ctx: CommandContext) -> CommandResult:
        value = not getattr(ctx.session, attr)
        setattr(ctx.session, attr, value)
        label, style, hint = ((on_label, on_style, on_hint) if value
                              else (off_label, off_style, off_hint))
        ctx.console.print(f"{name}: [{style}]{label}[/] [dim]({hint})[/]")
        if after is not None:
            after(ctx, value)
        return CommandResult(handled=True)

    return handler


_write = _toggle(
    "write_enabled", "write tools",
    on_hint="write_file, edit_file, bash enabled — /write to disable",
    off_hint="read-only; /write to enable file creation/edits",
)


def _bash_note(ctx: CommandContext, value: bool) -> None:
    if value and not ctx.session.write_enabled:
        ctx.console.print("[yellow]· note: bash is only exposed in write mode — "
                          "run /write to enable it[/]")


_bash_mode = _toggle(
    "unrestricted_bash", "shell",
    on_label="UNRESTRICTED", off_label="allowlisted",
    on_style="red", off_style="green",
    on_hint="any command — chains, pipes, redirects, venv/pip/build/test; "
            "cwd=repo root, NOT sandboxed",
    off_hint="safe binaries only; /bash for unrestricted dev mode",
    after=_bash_note,
)

# Live streaming of the model's thinking (B2), independent of /verbose.
_reasoning = _toggle(
    "show_reasoning", "reasoning",
    on_hint="streams model prose live; responsiveness tracks the backend's "
            "streaming cadence",
    off_hint="hidden",
)

# Terse model output (B2). Default ON; cuts wordy prose to save tokens — hence
# green for ON and yellow for OFF, the inverse of the other toggles.
_terse = _toggle(
    "terse", "terse",
    on_style="green", off_style="yellow",
    on_hint="report only deltas; tool output and errors are untouched — "
            "/terse to disable",
    off_hint="full prose",
)

# Compact display (WS4): tightens the on-screen ceiling for the model's final
# answer. Independent of /verbose (full) and /terse (model prose).
_compact_mode = _toggle(
    "compact", "compact",
    on_hint="tighter on-screen output ceiling; /verbose full or /debug for "
            "everything",
    off_hint="default truncated output",
)


_VERBOSE_LEVELS = ("off", "diff", "full")


def _web_mode(args, ctx: CommandContext) -> CommandResult:
    """Toggle the web tool surface (default OFF).

    Independent of /write on purpose: fetching a page mutates nothing on
    disk, so tying it to the file-write gate would be a category error. It is
    off by default because luxe is the offline fallback kit — network egress
    from a tool is a capability you opt into, not one you inherit.
    """
    from luxe.web import search as search_mod

    ctx.session.web_enabled = not ctx.session.web_enabled
    if not ctx.session.web_enabled:
        # A live interactive page IS network egress — it dies with the gate.
        from luxe.web.page import close_session
        close_session()
        ctx.console.print("web tools: [green]OFF[/] "
                          "[dim](no network egress from tools; /web to enable)[/]")
        return CommandResult(handled=True)

    ctx.console.print("web tools: [yellow]ON[/] "
                      "[dim](web_fetch — public http/https only; private, "
                      "loopback and tailnet hosts are refused)[/]")
    if search_mod.configured():
        provider = search_mod.active_provider()
        name = provider[0].name if provider else "?"
        ctx.console.print(f"  [green]·[/] web_search available [dim](via {name})[/]")
    else:
        ctx.console.print("  [yellow]·[/] web_search withheld [dim]— "
                          "no provider key found (see /doctor)[/]")
    # Answers is a separate product on a separate key — report it separately.
    from luxe.web.answers import configured as _answers_configured
    if _answers_configured():
        ctx.console.print("  [green]·[/] web_answer available [dim](brave "
                          "answers — one grounded answer per question)[/]")
    else:
        ctx.console.print("  [yellow]·[/] web_answer withheld [dim]— "
                          "no BRAVE_ANSWERS_API_KEY (see /doctor)[/]")
    # State the render capability up front: the model is told to retry with
    # render=true on a JS-heavy page, and that advice is useless if Chromium
    # was never installed on this host.
    from luxe.web.browser import availability
    avail = availability()
    if avail.ok:
        ctx.console.print("  [green]·[/] headless render available "
                          "[dim](render=true)[/]")
        ctx.console.print("  [green]·[/] web_page available [dim](interactive "
                          "page session — open/click/type)[/]")
    else:
        ctx.console.print(f"  [yellow]·[/] headless render unavailable [dim]— "
                          f"{avail.reason}; fix: {avail.fix}[/]")
        ctx.console.print("  [yellow]·[/] web_page withheld [dim]— needs the "
                          "render fix above[/]")
    return CommandResult(handled=True)


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


def _debug(args, ctx: CommandContext) -> CommandResult:
    """Convenience (B6): one switch for "show everything" = verbose full +
    reasoning. Toggles both on together, or both off."""
    s = ctx.session
    fully_on = s.verbose_level == "full" and s.show_reasoning
    if fully_on:
        s.verbose_level = "off"
        s.show_reasoning = False
        ctx.console.print("debug: [green]OFF[/] [dim](verbose + reasoning off)[/]")
    else:
        s.verbose_level = "full"
        s.show_reasoning = True
        ctx.console.print("debug: [red]ON[/] [dim](verbose full + reasoning — "
                          "full tool I/O, file contents, and live thinking)[/]")
    return CommandResult(handled=True)
