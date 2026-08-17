"""Session switches: `/theme` `/use` `/ctx` and the on/off toggles.

`/write` `/bash` `/web` `/verbose` `/reasoning` `/debug` `/terse` `/compact`.
Split out of `commands.py` 2026-08-04 (behavior unchanged); the five handlers
that were the same eight lines with different words now come from `_toggle`,
which reproduces their output byte for byte.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from luxe.chat.commands import _SLOTS, CommandContext, CommandResult
from luxe.chat.session import (
    CTX_TIER_MIN_RAM_GB,
    CTX_TIERS,
    ctx_tier_ram_warning,
    parse_ctx_size,
    tier_label,
)


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


def _ctx_on_local_ram(ctx: CommandContext) -> bool:
    """Whether the chat model's KV cache lives in THIS machine's RAM.

    The per-tier RAM warnings (`CTX_TIER_MIN_RAM_GB`) are arithmetic about the
    local KV cache — ~80 KiB/token of weights-adjacent memory on this box.
    Against a hosted endpoint they describe hardware that isn't in the loop,
    so a 1M-token model would be reported as needing a machine the user does
    not have to reach a window the provider is already serving. Guarded: an
    origin lookup that fails means "assume local", the pre-2026-08-17 answer.
    """
    from luxe.chat import origin as origin_mod

    try:
        org = origin_mod.cached_origin_for(ctx.slots.backend,
                                           ctx.slots.model_for("chat"))
        return org.kind != "remote"
    except Exception:
        return True


def _ctx(args, ctx: CommandContext) -> CommandResult:
    # Display against the conversational `chat` slot (the default route).
    ceiling = ctx.slots.ctx_ceiling("chat")
    base = ctx.slots.default_num_ctx("chat")
    active = ctx.session.num_ctx_override or base
    local_ram = _ctx_on_local_ram(ctx)
    try:
        billable = ctx.slots.active_entry().is_billable()
    except Exception:
        billable = False

    def _tiers_line() -> str:
        bits = []
        for name, n in CTX_TIERS.items():
            if n > ceiling:
                mark = "[dim](>max)[/]"
            elif local_ram and ctx_tier_ram_warning(name):
                # Inside the model's ceiling, past this HOST's RAM.
                mark = f"[yellow](needs {CTX_TIER_MIN_RAM_GB[name]}GB)[/]"
            else:
                mark = ""
            bits.append(f"{name} [dim]{n}[/]{mark}")
        return "  ".join(bits)

    if not args:
        eff = min(active, ceiling)
        clamp = f" [dim](clamped from {active})[/]" if eff != active else ""
        ctx.console.print(
            f"context window: [cyan]{tier_label(eff)}[/] [dim]num_ctx {eff}[/]{clamp}"
            f"  [dim]· max {ceiling}[/]")
        ctx.console.print(f"[dim]tiers:[/] {_tiers_line()}")
        if billable:
            # The cost framing replaces the RAM framing: nothing here is spent
            # on this machine's memory, and what the window really buys is how
            # much history each step carries — which is metered every step.
            ctx.console.print(
                "[dim]Bigger windows carry more billable prompt tokens (the "
                "window sets how much history each step re-sends). Set with "
                "/ctx <tier> or an absolute size: /ctx 500k, /ctx 1m.[/]")
        else:
            ctx.console.print("[dim]Bigger windows hold more code but cost "
                              "KV-cache RAM and tokens. Set with /ctx <tier> "
                              "or an absolute size: /ctx 65536, /ctx 128k.[/]")
        return CommandResult(handled=True)

    tier = args[0].lower()
    # A named tier, else an absolute size (`500k`, `1m`, `32768`) — the ladder
    # tops out at 256K and a hosted model can serve 1M.
    if tier in CTX_TIERS:
        requested = CTX_TIERS[tier]
        label = tier
    else:
        requested = parse_ctx_size(tier)
        if requested is None:
            ctx.console.print(
                f"[yellow]Unknown size {tier!r}; expected "
                f"{'|'.join(CTX_TIERS)}, or a token count like 65536, 500k, "
                "1m.[/]")
            return CommandResult(handled=True)
        label = tier_label(requested)

    ctx.session.num_ctx_override = requested
    eff = min(requested, ceiling)
    # Host-RAM warning, separate from the ceiling clamp above it: that ceiling
    # comes from what the MODEL supports, and on this box `huge` is inside it
    # while being past what the hardware can actually hold. Skipped entirely
    # when the weights aren't on this machine's RAM.
    ram_warning = ctx_tier_ram_warning(tier) if local_ram else None
    if ram_warning and eff == requested:
        ctx.console.print(f"[yellow]⚠[/] {ram_warning}")
    if eff != requested:
        limit_note = ("this model's window as the endpoint reports it"
                      if ctx.slots.catalog_context_length(
                          ctx.slots.model_for("chat"))
                      else "this box's max; raise num_ctx_max in the config "
                           "to go higher")
        ctx.console.print(
            f"[yellow]✓[/] context → [cyan]{label}[/] requested ({requested}), "
            f"[yellow]clamped to {eff}[/] [dim]({limit_note})[/]")
    else:
        ctx.console.print(f"[green]✓[/] context → [cyan]{label}[/] "
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

# Live streaming of the model's thinking (B2), independent of /verbose. Now a
# SUBCOMMAND of `/reasoning` (`show`/`hide`) rather than the bare command — see
# `_reasoning` below, which grew the effort control in 2026-08-17.
_reasoning_display = _toggle(
    "show_reasoning", "reasoning",
    on_hint="streams model prose live; responsiveness tracks the backend's "
            "streaming cadence",
    off_hint="hidden",
)


def _reasoning_state(ctx: CommandContext) -> str:
    """The effort setting in force on the ACTIVE backend.

    Read from the live Backend's `body_extras` — the thing that actually goes
    on the wire — rather than from a session field that could drift from it.
    """
    from luxe.config import REASONING_EFFORTS

    extras = getattr(ctx.slots.backend, "body_extras", None) or {}
    block = extras.get("reasoning")
    if not isinstance(block, dict):
        return "default"
    if block.get("exclude"):
        return "off"
    effort = block.get("effort")
    return effort if effort in REASONING_EFFORTS else "default"


def _reasoning(args, ctx: CommandContext) -> CommandResult:
    """Reasoning-model controls (chat-only).

    `/reasoning`                    show the effort setting + display state
    `/reasoning low|medium|high`    how hard the model should think
    `/reasoning off`                ask the provider not to return reasoning
    `/reasoning default`            send nothing; the provider decides
    `/reasoning show|hide`          stream the model's prose live (was the
                                    bare `/reasoning` toggle)

    Effort is a COST control as much as a quality one: a thinking model bills
    every reasoning token, and a one-sentence question measured 3,568
    characters of reasoning against 255 of answer (2026-08-17). It rewrites
    `body_extras` on the Backend this session owns — chat owns that instance,
    and the loop's `backend.chat` call site stays frozen.
    """
    from luxe.config import REASONING_SETTINGS, reasoning_extras

    if args and args[0].lower() in ("show", "hide"):
        want = args[0].lower() == "show"
        if ctx.session.show_reasoning == want:
            ctx.console.print(f"reasoning display: already "
                              f"[cyan]{'shown' if want else 'hidden'}[/]")
            return CommandResult(handled=True)
        return _reasoning_display([], ctx)

    try:
        entry = ctx.slots.active_entry()
        supported = entry is not None and entry.is_openrouter()
        engine = ctx.slots.engine_label()
    except Exception:
        supported, engine = False, "this backend"

    if not args:
        shown = "shown" if ctx.session.show_reasoning else "hidden"
        ctx.console.print(
            f"reasoning: effort [cyan]{_reasoning_state(ctx)}[/] "
            f"[dim]· live display {shown}[/]")
        if supported:
            ctx.console.print("[dim]/reasoning low|medium|high · off (don't "
                              "return it) · default (provider's own) · "
                              "show|hide (live display)[/]")
            ctx.console.print("[dim]Every reasoning token is billed, and it "
                              "is not part of the answer.[/]")
        else:
            ctx.console.print(f"[dim]effort has no effect on {engine} — it is "
                              "an OpenRouter request field. `show|hide` still "
                              "works.[/]")
        return CommandResult(handled=True)

    setting = args[0].lower()
    if setting not in REASONING_SETTINGS:
        ctx.console.print(
            f"[yellow]Unknown reasoning setting {setting!r}; expected "
            f"{'|'.join(REASONING_SETTINGS)}, or show|hide.[/]")
        return CommandResult(handled=True)
    if not supported:
        # Refuse rather than silently store it: a setting that changes nothing
        # is worse than a message saying so (chat.sdd — every refusal names
        # what would work).
        ctx.console.print(
            f"[yellow]· reasoning effort has no effect on {engine}[/] "
            "[dim]— it is an OpenRouter request field. `/backend` to switch, "
            "or `/reasoning show` to stream what this model does emit.[/]")
        return CommandResult(handled=True)

    extras = getattr(ctx.slots.backend, "body_extras", None)
    if extras is None:
        ctx.console.print("[yellow]This backend can't carry request extras.[/]")
        return CommandResult(handled=True)
    block = reasoning_extras(setting)
    if block is None:
        extras.pop("reasoning", None)
    else:
        extras["reasoning"] = block
    note = {"off": "the provider won't return reasoning",
            "default": "the provider's own default applies"}.get(
                setting, "billed per reasoning token")
    ctx.console.print(f"[green]✓[/] reasoning effort → [cyan]{setting}[/] "
                      f"[dim]({note}; applies to the next turn)[/]")
    return CommandResult(handled=True)

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


def _ephemeral(args, ctx: CommandContext) -> CommandResult:
    """Toggle write-nothing mode mid-session (`--ephemeral` at startup).

    Turning it ON has a problem the startup flag does not: the session
    directory already holds a transcript of everything said so far. Leaving it
    would be the opposite of what was asked, so this PURGES this session's own
    `~/.luxe/sessions/<id>/` and `~/.luxe/runs/<id>-*/` and says exactly what
    it removed. It does NOT touch `<repo>/.luxe/memory.md` — that file mixes
    machine blocks with the user's own curated text, and a mode that writes
    nothing must not become one that deletes hand-written notes; anything
    already spliced there is reported instead.

    Turning it OFF resumes persistence from the next write. The turns that
    passed while it was on are simply absent — there is no un-forget.
    """
    from luxe import ephemeral as eph

    arg = (args[0].lower() if args else "")
    if arg in ("on", "off"):
        want = arg == "on"
    elif arg:
        ctx.console.print(f"[yellow]Unknown option {arg!r}; expected on|off.[/]")
        return CommandResult(handled=True)
    else:
        want = not eph.is_ephemeral()

    if want == eph.is_ephemeral():
        state = "ON" if want else "OFF"
        ctx.console.print(f"[dim]ephemeral is already {state}.[/]")
        return CommandResult(handled=True)

    if not want:
        eph.disable()
        ctx.console.print(
            "ephemeral: [red]OFF[/] [dim](writes resume from the next turn; "
            "the turns taken while it was on were never recorded and do not "
            "come back)[/]")
        return CommandResult(handled=True)

    # ON: stop the debug log FIRST — the handler holds debug.log open inside
    # the directory about to be removed.
    from luxe.chat import debuglog
    if getattr(ctx, "session_log", None) is not None:
        debuglog.uninstall(ctx.session_log)
    eph.enable()
    removed = eph.purge_session(ctx.session.session_id,
                                getattr(ctx.session, "repo_path", "") or "")
    ctx.console.print("ephemeral: [yellow]ON[/] [dim](no transcript, no "
                      "debug.log, no run events, no project-memory writes; "
                      "write tools unaffected)[/]")
    if removed:
        ctx.console.print(f"[dim]· removed {len(removed)} path(s) this session "
                          f"had already written:[/]")
        for p in removed:
            ctx.console.print(f"[dim]    {p}[/]")
    repo = getattr(ctx.session, "repo_path", "") or ""
    if repo and (Path(repo) / ".luxe" / "memory.md").is_file():
        ctx.console.print(
            "[yellow]·[/] [dim]note: <repo>/.luxe/memory.md exists and was NOT "
            "touched — it holds your curated text alongside luxe's blocks. "
            "No further writes will be made to it.[/]")
    return CommandResult(handled=True)


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
