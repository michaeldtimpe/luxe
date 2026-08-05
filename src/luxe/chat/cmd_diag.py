"""Read-only diagnostics: `/status` `/tools` `/doctor` `/outage` `/net` `/planeproxy`.

Split out of `commands.py` 2026-08-04 (behavior unchanged). Every command here
reports state and changes none of it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from luxe import textfmt
from luxe.chat import modelcaps
from luxe.chat import origin as origin_mod
from luxe.chat.commands import CommandContext, CommandResult, _usage


def _status(args, ctx: CommandContext) -> CommandResult:
    """One-screen session summary — the things spread across the status bar,
    the banner, and `/model`, in one place."""
    from luxe.modelstore import human_bytes

    s, sm = ctx.session, ctx.slots
    model = sm.model_for("chat")
    org = origin_mod.cached_origin_for(sm.backend, model)
    if org.kind == "unknown":
        try:
            org = origin_mod.origin_for(sm.backend, model)
        except Exception:
            pass

    kind_label = {"git": "git repo", "dir": "project", "none": "no project"}.get(
        s.project_kind, s.project_kind)
    rows = [
        ("project", f"{s.repo_path or '(none)'}  ({kind_label})"),
        ("session", s.session_id or "(unsaved)"),
        ("backend", f"{sm.backend_name} · {getattr(sm.backend, 'base_url', '?')}"),
        ("model", f"{model}  {org.glyph} {org.label}"),
        ("weights", org.detail or "(unreported)"),
        ("resident", sm.resident or "(none loaded yet)"),
        ("slot", "chat/plan/code → " + ", ".join(
            f"{k}:{v}" for k, v in sm.slot_models().items())),
        ("context", f"{s.num_ctx_override or sm.role_for('chat').num_ctx} tokens"
                    f" (ceiling {sm.ctx_ceiling('chat')})"),
        ("mode", f"write {'on' if s.write_enabled else 'off'} · "
                 f"bash {'unrestricted' if s.unrestricted_bash else 'allowlisted'} · "
                 f"terse {'on' if s.terse else 'off'} · "
                 f"verbose {s.verbose_level}"),
        ("turns", str(len(s.turns))),
        ("swaps", f"{sm.stats.count} ({sm.stats.seconds:.0f}s)"),
    ]
    if s.attachments:
        rows.append(("attached", f"{len(s.attachments)} file(s) for the next turn"))
    try:
        free = shutil.disk_usage(Path.home()).free
        rows.append(("disk free", human_bytes(free)))
    except OSError:
        pass

    width = max(len(k) for k, _ in rows)
    ctx.console.print("[bold]Session[/]")
    for key, val in rows:
        ctx.console.print(f"  [dim]{key.ljust(width)}[/]  {val}")
    return CommandResult(handled=True)


def _tools(args, ctx: CommandContext) -> CommandResult:
    """List the tool surface the model will actually get on the next turn.

    Read-only mode STRIPS write tools rather than hiding a missing capability
    (lessons.md 2026-06-01) — so the gated ones are listed too, with the toggle
    that restores them. What the model sees is what this prints.
    """
    from rich.markup import escape

    from luxe.mcp.server import _MUTATION_TOOL_NAMES

    cap = modelcaps.for_model(ctx.slots.backend, ctx.slots.model_for("chat"))
    if not cap.usable:
        ctx.console.print("[bold]Tools this turn[/] [yellow](none)[/]")
        ctx.console.print(f"  [yellow]⚠ {ctx.slots.model_for('chat')} cannot "
                          f"call tools[/] [dim]— {cap.reason}[/]")
        ctx.console.print("  [dim]switch with `/model chat <id>` to get the tool "
                          "surface back[/]")
        return CommandResult(handled=True)

    role = ctx.slots.role_for("chat")
    configured = list(role.tools or [])
    write_on = ctx.session.write_enabled
    active = [t for t in configured if write_on or t not in _MUTATION_TOOL_NAMES]
    gated = [t for t in configured if t not in active]

    ctx.console.print(f"[bold]Tools this turn[/] [dim]({len(active)} active)[/]")
    for t in active:
        note = ""
        if t == "bash":
            note = ("  [dim](unrestricted dev shell)[/]" if ctx.session.unrestricted_bash
                    else "  [dim](allowlisted commands — /bash for unrestricted)[/]")
        ctx.console.print(f"  [green]·[/] {t}{note}")
    # Always-on extras registered on the chat seam (repl.prepare_turn) —
    # keep this list honest with what the seam actually appends.
    for extra, note in (
        ("update_ledger", "always on"),
        ("net_probe", "always on — bounded network ladder"),
        ("planeproxy_diag", "always on — read-only tunnel diagnosis"),
    ):
        # escape(): a note naming an extra like "[web]" is valid Rich markup,
        # and unescaped it renders as "needs  extra" with the name swallowed.
        ctx.console.print(f"  [green]·[/] {extra}  [dim]({escape(note)})[/]")
    # The /web surface is gated, so it is listed separately from the
    # always-on extras above.
    if ctx.session.web_enabled:
        from luxe.web.answers import configured as _answers_configured
        from luxe.web.search import configured as _search_configured
        ctx.console.print("  [green]·[/] web_fetch  [dim](/web on)[/]")
        if _search_configured():
            ctx.console.print("  [green]·[/] web_search  [dim](/web on)[/]")
        else:
            ctx.console.print("  [yellow]·[/] web_search  "
                              "[dim](withheld — no provider API key)[/]")
        if _answers_configured():
            ctx.console.print("  [green]·[/] web_answer  [dim](/web on — "
                              "grounded answers, separate from search)[/]")
        else:
            ctx.console.print("  [yellow]·[/] web_answer  "
                              "[dim](withheld — no BRAVE_ANSWERS_API_KEY)[/]")
    else:
        ctx.console.print("  [yellow]·[/] web_fetch, web_search, web_answer  "
                          "[dim](off — /web to enable)[/]")
    if gated:
        ctx.console.print(f"[bold]Gated by read-only mode[/] [dim](/write "
                          f"enables {len(gated)})[/]")
        for t in gated:
            ctx.console.print(f"  [yellow]·[/] {t}")

    # MCP tools (cli --mcp): the inspection surface is always on; tools the
    # server config marks mutating (`gate_tools`) follow the /write gate.
    from luxe.chat import mcptools
    surf = mcptools.active()
    if surf is not None:
        mcp_active = list(surf.always_defs) + (list(surf.gated_defs) if write_on else [])
        mcp_gated = [] if write_on else list(surf.gated_defs)
        ctx.console.print(f"[bold]MCP tools[/] [dim]({len(mcp_active)} active)[/]")
        for d in mcp_active:
            ctx.console.print(f"  [green]·[/] {d.name}")
        if mcp_gated:
            ctx.console.print(f"[bold]MCP gated by read-only mode[/] [dim](/write "
                              f"enables {len(mcp_gated)})[/]")
            for d in mcp_gated:
                ctx.console.print(f"  [yellow]·[/] {d.name}")
        if surf.status_fn is not None:
            for s in surf.status_fn():
                if s.get("down"):
                    ctx.console.print(f"  [yellow]⚠ server {s['name']} DOWN[/] "
                                      f"[dim]— {s['down_reason']}[/]")
    else:
        # Say it out loud. There is NO way to attach a server mid-session, so
        # a user telling the model "use the kappa relay" in a plain session
        # would otherwise get an unexplained "I have no such tool".
        ctx.console.print("[bold]MCP tools[/] [dim](none)[/]")
        ctx.console.print("  [dim]MCP servers attach at STARTUP only — restart "
                          "with `luxe chat --mcp <name>` (servers come from "
                          "`--mcp-config`, default configs/mcp.yaml)[/]")
    return CommandResult(handled=True)


def _doctor(args, ctx: CommandContext) -> CommandResult:
    """Preflight the session: endpoint, key, model + weight origin, disk, index
    freshness, git state, mode, TUI. Every WARN/FAIL carries the fix."""
    from luxe.chat import inspection

    doc = inspection.run_doctor(ctx.session, ctx.slots, ctx.session.repo_path)
    # Rendering is shared with `luxe ready` (inspection.render_doctor) so the
    # in-session and host-level tables can never drift apart.
    inspection.render_doctor(doc, ctx.console)
    verdict = {inspection.OK: "[green]all clear[/]",
               inspection.WARN: "[yellow]usable, with caveats above[/]",
               inspection.FAIL: "[red]something is broken — fix the ✗ first[/]"}
    ctx.console.print(f"  {verdict[doc.worst]}")
    return CommandResult(handled=True)


def _outage(args, ctx: CommandContext) -> CommandResult:
    """Print the offline emergency card (OUTAGE.md) into the session.

    Same bytes as `luxe outage` — one reader (`luxe.outage.load_card`), zero
    network, zero model. The point is that the card is reachable from INSIDE
    a session too, when the operator is already in the tool and can't
    remember the flag they need."""
    from rich.markdown import Markdown

    from luxe.outage import load_card

    text = load_card()
    try:
        ctx.console.print(Markdown(text))
    except Exception:            # never let rendering hide the card
        ctx.console.print(text)
    return CommandResult(handled=True)


def _net(args, ctx: CommandContext) -> CommandResult:
    """Deterministic layered network report (no model in the loop): the
    public DNS→TCP→TLS→HTTP ladder + captive-portal check + every configured
    `backends:` endpoint. Lives here, NOT in /doctor — doctor's offline-purity
    contract allows exactly one networked line (chat.sdd). Bounded: every
    probe carries a hard deadline; worst case is a few seconds."""
    from luxe import netdiag

    host = args[0] if args else netdiag.ANCHOR_HOST
    ctx.console.print(f"[dim]probing {host} + configured backends "
                      "(bounded, a few seconds)…[/]")
    try:
        report = netdiag.full_report(ctx.slots.cfg, host=host)
    except Exception as e:
        ctx.console.print(f"[red]✗ net report failed: {e}[/]")
        return CommandResult(handled=True)
    textfmt.render_ok_lines(ctx.console, netdiag.render_lines(report))
    style = "green" if report.ladder.verdict == netdiag.V_OK else "yellow"
    ctx.console.print(f"[{style}]verdict: {report.ladder.verdict}[/] — "
                      f"{report.ladder.advice}")
    return CommandResult(handled=True)


def _planeproxy(args, ctx: CommandContext) -> CommandResult:
    """Deterministic planeproxy diagnosis (no model in the loop): runs the
    tool's own read-only `status --json` / `doctor --json` under a hard
    deadline and prints the doctor checks, tunnel + isolation state, and a
    classified verdict with the fix. READ-ONLY by contract (chat.sdd): this
    command never runs `up`/`down` — starting or stopping the tunnel stays
    with the user."""
    from luxe import planeproxy

    check = (args[0].lower() if args else "both")
    if check not in ("status", "doctor", "both"):
        return _usage(ctx, "/planeproxy")
    ctx.console.print("[dim]probing planeproxy (read-only, bounded — a few "
                      "seconds)…[/]")
    try:
        report = planeproxy.full_report(check=check)
    except Exception as e:
        ctx.console.print(f"[red]✗ planeproxy report failed: {e}[/]")
        return CommandResult(handled=True)
    textfmt.render_ok_lines(ctx.console, planeproxy.render_lines(report))
    return CommandResult(handled=True)
