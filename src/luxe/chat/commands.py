"""Slash-command parsing + dispatch for the chat REPL.

Commands are decoupled from the loop via a `CommandContext` carrying the
session, the slot manager, the console, and injected hooks for the heavier
features (compare, resume) that the REPL wires in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rich.console import Console

from luxe.chat import modelcaps
from luxe.chat import origin as origin_mod
from luxe.chat.session import CTX_TIERS, ChatSession, tier_label
from luxe.chat.slots import SlotManager
from luxe.memory import project as project_mem

_SLOTS = ("chat", "plan", "code")


@dataclass
class CommandResult:
    handled: bool
    exit: bool = False
    # A message the front-end should run as a turn once the command returns
    # (`/retry`). Kept as data rather than a callback so both front-ends —
    # line REPL and TUI — act on it in their own turn machinery.
    submit: str = ""


@dataclass
class CommandContext:
    console: Console
    session: ChatSession
    slots: SlotManager
    on_compare: Callable[[str], None] | None = None
    on_compare_review: Callable[[str], None] | None = None
    on_resume: Callable[[str], None] | None = None
    on_git_analysis: Callable[[str, "bool | None"], None] | None = None  # (kind, deep) -> run gitkit report
    # (path|None) -> summary dict. Re-resolves the project, rebuilds the index,
    # and moves the repo lock; provided by cli.chat_cmd (it owns the lock).
    on_project: Callable[["str | None"], dict] | None = None
    # The live status-bar snapshot, so commands that invalidate it can say so
    # (`/clear` must clear ctx%/cache — they describe a conversation that's gone).
    status: object | None = None


# (command, args, description) — rendered into an auto-aligned table by _help()
# so every description starts at the same column regardless of command width.
_HELP_ROWS: list[tuple[str, str, str]] = [
    ("/help", "", "show this help"),
    ("/model", "[slot] [model_id]", "show slots, or repoint chat|plan|code"),
    ("/backend", "[name|n]", "list configured oMLX backends, or switch to one"),
    ("/pull", "[repo|name] [--search q] [--yes]", "fetch model weights (mount → HF)"),
    ("/use", "<slot>", "pin the next turn to chat|plan|code"),
    ("/unload", "", "free oMLX RAM (unload resident weights) without quitting"),
    ("/status", "", "session summary: repo, backend, model origin, usage"),
    ("/tools", "", "list the tools the model has THIS turn (and what's gated)"),
    ("/theme", "[preview|auto|cool|warm|mono]", "preview or switch the colour palette"),
    ("/retry", "", "re-run your last message (e.g. after /write or /ctx)"),
    ("/project", "[path]", "show, attach, or switch the project (re-indexes)"),
    ("/index", "[path]", "build the code-search index for here (or <path>)"),
    ("/doctor", "", "preflight this session: endpoint, model, index, disk, git"),
    ("/diff", "[--full] [--all]", "what this session changed (git-backed)"),
    ("/export", "[path]", "write the conversation to markdown"),
    ("/ctx", "[small|medium|large|xlarge|huge]", "show or set context window size"),
    ("/write", "", "toggle write tools (default: read-only)"),
    ("/bash", "", "toggle unrestricted shell (default: allowlisted)"),
    ("/verbose", "[diff|full|off]", "show full tool I/O (diffs, file contents, results)"),
    ("/reasoning", "", "toggle live streaming of the model's thinking"),
    ("/debug", "", 'toggle "show everything" (verbose full + reasoning)'),
    ("/terse", "", "toggle terse model output (default ON; saves tokens)"),
    ("/compact", "", "toggle compact display (tighter on-screen output ceiling)"),
    ("/goal", "<objective> | stop", "autonomously run rounds until the objective is met"),
    ("/plan", "<objective>", "draft a plan, then choose: save / execute / both"),
    ("/attach", "<path> [...]", "attach file contents to the NEXT turn (one-shot)"),
    ("/sys", "[add <rule>|list|clear]", "manage session-scoped system constraints"),
    ("/memory", "list|add|promote|forget|edit", "manage project memory"),
    ("/gitaudit", "", "audit this repo: orientation + bugs/security + structural advice"),
    ("/gitchange", "", "produce an apply-ready structural change plan for this repo"),
    ("/compare", "<task>", "run two configs side-by-side"),
    ("/compare review", "[id]", "replay a stored comparison"),
    ("/resume", "[id]", "resume a prior session (or list them)"),
    ("/clear", "", "start a fresh conversation"),
    ("/quit", "", "exit (Ctrl-D also works)"),
]


def is_command(line: str) -> bool:
    return line.strip().startswith("/")


def dispatch(line: str, ctx: CommandContext) -> CommandResult:
    parts = line.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]
    handlers = {
        "/help": _help,
        "/model": _model,
        "/backend": _backend,
        "/pull": _pull,
        "/unload": _unload,
        "/status": _status,
        "/tools": _tools,
        "/theme": _theme,
        "/retry": _retry,
        "/project": _project,
        "/index": _index_cmd,
        "/doctor": _doctor,
        "/diff": _diff,
        "/export": _export,
        "/use": _use,
        "/ctx": _ctx,
        "/write": _write,
        "/bash": _bash_mode,
        "/verbose": _verbose,
        "/reasoning": _reasoning,
        "/debug": _debug,
        "/terse": _terse,
        "/compact": _compact_mode,
        "/goal": _goal,
        "/plan": _plan,
        "/attach": _attach,
        "/sys": _sys,
        "/memory": _memory,
        "/gitaudit": _gitaudit, "/git-audit": _gitaudit, "/gaudit": _gitaudit,
        "/gitchange": _gitchange, "/git-change": _gitchange, "/gchange": _gitchange,
        # back-compat aliases → the two merged commands
        "/gitsummary": _gitaudit, "/gsum": _gitaudit,
        "/gitreview": _gitaudit, "/grev": _gitaudit,
        "/gitrefactor": _gitaudit, "/gref": _gitaudit,
        "/gitplan": _gitchange, "/gplan": _gitchange,
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
    from rich.markup import escape
    from rich.table import Table

    ctx.console.print("[bold]luxe chat commands[/]")
    # box=None + a sized command column → descriptions line up in one column.
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column("command", no_wrap=True)
    table.add_column("description", overflow="fold")
    for name, cmd_args, desc in _HELP_ROWS:
        sig = f"[cyan]{name}[/]"
        if cmd_args:
            # escape literal []-placeholders so Rich doesn't eat them as markup
            sig += f" [dim]{escape(cmd_args)}[/]"
        table.add_row(sig, escape(desc))
    ctx.console.print(table)
    return CommandResult(handled=True)


def _model(args, ctx: CommandContext) -> CommandResult:
    """Show/repoint the chat|plan|code model slots.

    `/model`                list slots + a numbered list of available oMLX models
    `/model <slot>`         show that slot's model
    `/model <slot> <n>`     point the slot at the n-th available model
    `/model <slot> <id>`    point the slot at an explicit model id
    """
    if not args:
        slot_models = ctx.slots.slot_models()
        ctx.console.print(f"[bold]Model slots[/] [dim](resident in RAM: "
                          f"[cyan]{ctx.slots.resident}[/])[/]")
        for slot, model in slot_models.items():
            ctx.console.print(f"  [cyan]{slot:5s}[/] → {model}")
        avail = ctx.slots.available_models()
        if avail:
            in_use = set(slot_models.values())
            # Provenance per model (chat/origin.py) so "which of these is
            # actually on this disk?" is answerable at selection time.
            try:
                origins = origin_mod.origins_for_backend(ctx.slots.backend)
            except Exception:
                origins = {}
            ctx.console.print("[dim]available models — `/model <slot> <n>`:[/]")
            for i, m in enumerate(avail, 1):
                marks = []
                if m == ctx.slots.resident:
                    marks.append("resident")
                if m in in_use:
                    marks.append("in use")
                tag = f"  [dim]({', '.join(marks)})[/]" if marks else ""
                org = origins.get(m)
                mark = ""
                # Local is the norm — no marker for it (absence of ☁/⇅ IS
                # "local", per the 2026-07-30 roster trim). Only a wire crossing
                # is worth a glyph.
                if org is not None and org.is_over_the_network:
                    mark = f" [yellow]{org.glyph} {org.label}[/]"
                if not modelcaps.for_model(ctx.slots.backend, m).usable:
                    mark += " [yellow]⚠ no tool support[/]"
                ctx.console.print(f"  [cyan]{i:2d}[/] {m}{mark}{tag}")
            if any(o.is_over_the_network for o in origins.values()):
                ctx.console.print("[dim]  ☁ network volume · ⇅ remote endpoint "
                                  "(unmarked = local disk)[/]")
        else:
            ctx.console.print("[dim](oMLX unreachable — `/model <slot> <id>` "
                              "still works)[/]")
        return CommandResult(handled=True)
    slot = args[0]
    if slot not in _SLOTS:
        ctx.console.print(f"[yellow]Unknown slot {slot!r}; expected chat|plan|code.[/]")
        return CommandResult(handled=True)
    if len(args) < 2:
        ctx.console.print(f"  {slot} → {ctx.slots.model_for(slot)}")
        return CommandResult(handled=True)
    sel = args[1]
    # Numeric selection indexes into the available-model list (1-based).
    if sel.isdigit():
        avail = ctx.slots.available_models()
        idx = int(sel)
        if not avail:
            ctx.console.print("[yellow]No available-model list (oMLX unreachable) "
                              "— pass an explicit id: /model <slot> <id>.[/]")
            return CommandResult(handled=True)
        if not (1 <= idx <= len(avail)):
            ctx.console.print(f"[yellow]Pick 1–{len(avail)} (see /model).[/]")
            return CommandResult(handled=True)
        model_id = avail[idx - 1]
    else:
        model_id = sel
    ctx.slots.set_override(slot, model_id)
    ctx.console.print(f"[green]✓[/] slot [cyan]{slot}[/] → {model_id} "
                      f"[dim](swaps on next {slot} turn)[/]")
    try:
        org = origin_mod.origin_for(ctx.slots.backend, model_id)
    except Exception:
        org = None
    if org is not None and org.is_over_the_network:
        ctx.console.print(f"  [yellow]{org.glyph} {org.describe()}[/]")
    cap = modelcaps.for_model(ctx.slots.backend, model_id)
    if not cap.usable:
        ctx.console.print(
            f"  [yellow]⚠ {model_id} cannot call tools[/] [dim]— {cap.reason}. "
            "luxe will withhold the tool surface on its turns: conversation "
            "only, no reading or editing files.[/]")
    return CommandResult(handled=True)


def _backend(args, ctx: CommandContext) -> CommandResult:
    """List or switch the session's oMLX endpoint (multi-backend, chat-only).

    `/backend`            list entries: name, base_url, health ✓/✗, active marker
    `/backend <name|n>`   switch (health-checked; never touches the old server)
    """
    from luxe.backend import BackendError

    entries = ctx.slots.cfg.backend_entries()
    names = list(entries)
    if not args:
        ctx.console.print("[bold]Backends[/]")
        for i, (name, entry) in enumerate(entries.items(), 1):
            ok = ctx.slots.probe_backend(name)
            health = "[green]✓[/]" if ok else "[red]✗[/]"
            active = " [cyan]← active[/]" if name == ctx.slots.backend_name else ""
            ctx.console.print(
                f"  [cyan]{i}[/] {name:8s} {entry.base_url}  {health}{active}")
        if len(names) > 1:
            ctx.console.print("[dim]switch with /backend <name|n>[/]")
        return CommandResult(handled=True)

    sel = args[0]
    if sel.isdigit():
        idx = int(sel)
        if not (1 <= idx <= len(names)):
            ctx.console.print(f"[yellow]Pick 1–{len(names)} (see /backend).[/]")
            return CommandResult(handled=True)
        name = names[idx - 1]
    else:
        name = sel
    if name not in entries:
        ctx.console.print(f"[yellow]Unknown backend {name!r}. "
                          f"Configured: {', '.join(names)}.[/]")
        return CommandResult(handled=True)
    if name == ctx.slots.backend_name:
        ctx.console.print(f"[dim]· already on backend [cyan]{name}[/][/]")
        return CommandResult(handled=True)
    try:
        dropped = ctx.slots.switch_backend(name)
    except BackendError as e:
        ctx.console.print(f"[red]✗ {e}[/] [dim](staying on "
                          f"{ctx.slots.backend_name})[/]")
        return CommandResult(handled=True)
    entry = entries[name]
    ctx.console.print(f"[green]✓[/] backend → [cyan]{name}[/] "
                      f"[dim]({entry.base_url}; timeout {entry.timeout_s:.0f}s)[/]")
    for slot in dropped:
        ctx.console.print(f"[yellow]· dropped /model override on slot "
                          f"[cyan]{slot}[/] — model not served here[/]")
    return CommandResult(handled=True)


def _pull(args, ctx: CommandContext) -> CommandResult:
    """Fetch model weights onto this machine (chat-side `luxe pull`).

    `/pull`                     local models + in-flight downloads
    `/pull --search <query>`    search HuggingFace for MLX models
    `/pull <repo|name>`         PREVIEW: where it would come from, and its size
    `/pull <repo|name> --yes`   actually transfer it
    `/pull <name> --from <dir>` import an explicit directory (mounted volume)

    The preview step is the consent step: a chat command has no confirmation
    prompt, and a pull can move tens of gigabytes. Transfers run on the command
    worker, so the REPL stays responsive and Esc still interrupts.
    """
    from luxe import modelstore as ms

    flags = {a for a in args if a.startswith("--")}
    positional = [a for a in args if not a.startswith("--")]
    from_path = ""
    if "--from" in args:
        i = args.index("--from")
        if i + 1 < len(args):
            from_path = args[i + 1]
            if from_path in positional:
                positional.remove(from_path)
    if "--search" in flags:
        query = " ".join(positional)
        if not query:
            ctx.console.print("[yellow]Usage: /pull --search <query>[/]")
            return CommandResult(handled=True)

    base_url = getattr(ctx.slots.backend, "base_url", "") or ""
    api_key = getattr(ctx.slots.backend, "api_key", "") or ""
    try:
        with ms.OmlxAdmin(base_url=base_url, api_key=api_key) as admin:
            if "--search" in flags:
                _pull_show_search(ctx, admin, " ".join(positional))
                return CommandResult(handled=True)
            if not positional and not from_path:
                _pull_show_state(ctx, admin)
                return CommandResult(handled=True)

            ref = positional[0] if positional else ms.store_name_for(from_path)
            name = ms.store_name_for(ref)
            if from_path:
                src = ms._resolve_hf_snapshot(Path(from_path).expanduser())
                if src is None:
                    ctx.console.print(f"[red]✗ {from_path} is not an MLX model "
                                      "directory (config.json + weights).[/]")
                    return CommandResult(handled=True)
                sources = [ms.ModelSource(kind="mount", ref=str(src), name=name,
                                          size_bytes=ms.dir_size(src), note="--from")]
            else:
                ctx.console.print("[dim]· looking for it (mounts, then HF)…[/]")
                sources = ms.resolve_sources(ref, admin=admin,
                                             include_mounts="--hf" not in flags)
            if not sources:
                ctx.console.print(
                    f"[red]✗ Nowhere to pull {ref!r} from.[/] [dim]Not on a "
                    "mounted volume; an HF fetch needs a full `org/Model` id. "
                    "Try `/pull --search <query>`.[/]")
                return CommandResult(handled=True)

            chosen = sources[0]
            already = name in ms.local_model_names()
            ctx.console.print(f"[bold]{name}[/] ← {chosen.describe()}")
            if already and "--force" not in flags:
                ctx.console.print("[yellow]· already in the local store "
                                  "— add --force to replace it[/]")
                return CommandResult(handled=True)
            if "--yes" not in flags:
                ctx.console.print(f"[dim]· preview only — run "
                                  f"`/pull {ref} --yes` to transfer[/]")
                return CommandResult(handled=True)

            if chosen.kind == "mount":
                _pull_copy(ctx, chosen, force="--force" in flags)
            else:
                _pull_download(ctx, admin, chosen)
    except ms.ModelStoreError as e:
        ctx.console.print(f"[red]✗ {e}[/]")
    return CommandResult(handled=True)


def _pull_show_state(ctx: CommandContext, admin) -> None:
    from luxe.modelstore import ModelStoreError, human_bytes, local_model_names

    names = local_model_names()
    ctx.console.print(f"[bold]Local models[/] [dim]({len(names)})[/]")
    for n in names:
        ctx.console.print(f"  · {n}")
    try:
        tasks = admin.tasks()
    except ModelStoreError as e:
        ctx.console.print(f"[dim]· download queue unavailable: {e}[/]")
        return
    for t in tasks:
        ctx.console.print(f"  ↓ {t.repo_id} — {t.status} {t.progress:.0f}% "
                          f"[dim]{human_bytes(t.downloaded_size)}/"
                          f"{human_bytes(t.total_size)}[/]")
    ctx.console.print("[dim]· `/pull <repo-id>` to preview a fetch[/]")


def _pull_show_search(ctx: CommandContext, admin, query: str) -> None:
    from luxe.modelstore import human_bytes

    hits = admin.search(query)
    if not hits:
        ctx.console.print(f"[yellow]No MLX models found for {query!r}.[/]")
        return
    for m in hits[:15]:
        size = f"  [dim]{human_bytes(m.size_bytes)}[/]" if m.size_bytes else ""
        ctx.console.print(f"  {m.repo_id}{size}  [dim]↓{m.downloads:,}[/]")
    ctx.console.print("[dim]· `/pull <repo-id> --yes` to fetch[/]")


def _pull_copy(ctx: CommandContext, source, *, force: bool) -> None:
    from luxe import modelstore as ms

    last = [0.0]

    def _tick(done: int, total: int) -> None:
        # One line per 10% — the chat log is a transcript, not a progress bar.
        pct = (done / total * 100) if total else 0
        if pct - last[0] >= 10:
            last[0] = pct
            ctx.console.print(f"[dim]  copying… {pct:.0f}% "
                              f"({ms.human_bytes(done)})[/]")

    res = ms.copy_into_store(source, force=force, on_progress=_tick)
    ctx.console.print(f"[green]✓[/] {res.name} → {res.dest} "
                      f"[dim]({ms.human_bytes(res.bytes_copied)} in "
                      f"{res.seconds:.0f}s)[/]")


def _pull_download(ctx: CommandContext, admin, source) -> None:
    from luxe.modelstore import human_bytes

    task = admin.start_download(source.ref)
    ctx.console.print(f"[dim]· oMLX download task {task.task_id}[/]")
    last = [0.0]

    def _tick(t) -> None:
        if t.progress - last[0] >= 10 or t.done:
            last[0] = t.progress
            ctx.console.print(f"[dim]  {t.status} {t.progress:.0f}% "
                              f"({human_bytes(t.downloaded_size)}/"
                              f"{human_bytes(t.total_size)})[/]")

    final = admin.wait_for(task.task_id, on_progress=_tick)
    if final.status == "completed":
        ctx.console.print(f"[green]✓[/] {final.repo_id} downloaded "
                          "[dim](/model to select it)[/]")
    else:
        ctx.console.print(f"[red]✗ {final.repo_id}: "
                          f"{final.error or final.status}[/]")


def _unload(args, ctx: CommandContext) -> CommandResult:
    """Free oMLX RAM mid-session (the CLI's `luxe unload`, without quitting).

    Useful before running something else on the box; the next turn reloads the
    model, so the only cost is one warm-up.
    """
    backend = ctx.slots.backend
    try:
        loaded = backend.loaded_models()
    except Exception as e:
        ctx.console.print(f"[red]✗ oMLX unreachable: {e}[/]")
        return CommandResult(handled=True)
    if not loaded:
        ctx.console.print("[dim]· nothing loaded — no RAM to free[/]")
        return CommandResult(handled=True)
    results = backend.unload_all_loaded()
    ok = [m for m, good in results.items() if good]
    for m in ok:
        ctx.console.print(f"[green]✓[/] unloaded {m}")
    for m, good in results.items():
        if not good:
            ctx.console.print(f"[yellow]✗ {m} — unload failed[/]")
    # Residency is now unknown to the slot manager; force a reconfirm.
    ctx.slots.forget_resident()
    ctx.console.print("[dim]· next turn reloads the model (one warm-up)[/]")
    return CommandResult(handled=True)


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
    ctx.console.print("  [green]·[/] update_ledger  [dim](always on)[/]")
    if gated:
        ctx.console.print(f"[bold]Gated by read-only mode[/] [dim](/write "
                          f"enables {len(gated)})[/]")
        for t in gated:
            ctx.console.print(f"  [yellow]·[/] {t}")
    return CommandResult(handled=True)


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
        ctx.console.print("[bold]Palettes[/]")
        for name in choices:
            mark = " [cyan]← active[/]" if name == current else ""
            hint = ("  [dim](tracks your terminal/statusline theme)[/]"
                    if name == "auto" else "")
            ctx.console.print(f"  {name}{hint}{mark}")
        ctx.console.print("[dim]switch with /theme <name>[/]")
        return CommandResult(handled=True)

    name = args[0].lower()
    if name not in choices:
        ctx.console.print(f"[yellow]Unknown palette {name!r}. "
                          f"Choose from: {', '.join(choices)}.[/]")
        return CommandResult(handled=True)
    theme_mod.set_palette(name)
    ctx.console.print(f"[green]✓[/] palette → [cyan]{name}[/]"
                      + ("  [dim](tracking your terminal theme)[/]"
                         if name == "auto" else ""))
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


def _retry(args, ctx: CommandContext) -> CommandResult:
    """Re-run the last user message — the natural follow-up to `/write`,
    `/ctx`, `/model`, or a turn that failed."""
    last = next((t.user for t in reversed(ctx.session.turns) if t.user), "")
    if not last:
        ctx.console.print("[yellow]Nothing to retry yet.[/]")
        return CommandResult(handled=True)
    preview = last if len(last) <= 60 else last[:59] + "…"
    ctx.console.print(f"[dim]· retrying:[/] {preview}")
    return CommandResult(handled=True, submit=last)


def _project(args, ctx: CommandContext) -> CommandResult:
    """Show, attach, or switch the project this session is about.

    `/project`          what's attached now (and whether it's indexed)
    `/project <path>`   attach/switch: re-resolve, re-index, move the repo lock

    A session started outside a project is a normal way to use luxe — this is
    how you give it a codebase without restarting.
    """
    from luxe.chat import project as project_mod
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


def _doctor(args, ctx: CommandContext) -> CommandResult:
    """Preflight the session: endpoint, key, model + weight origin, disk, index
    freshness, git state, mode, TUI. Every WARN/FAIL carries the fix."""
    from luxe.chat import inspection

    doc = inspection.run_doctor(ctx.session, ctx.slots, ctx.session.repo_path)
    glyphs = {inspection.OK: "[green]✓[/]", inspection.WARN: "[yellow]![/]",
              inspection.FAIL: "[red]✗[/]"}
    width = max((len(c.name) for c in doc.checks), default=0)
    ctx.console.print("[bold]Doctor[/]")
    for c in doc.checks:
        line = f"  {glyphs.get(c.state, '·')} [dim]{c.name.ljust(width)}[/]  {c.detail}"
        ctx.console.print(line)
        if c.fix and c.state != inspection.OK:
            ctx.console.print(f"    [dim]→ {c.fix}[/]")
    verdict = {inspection.OK: "[green]all clear[/]",
               inspection.WARN: "[yellow]usable, with caveats above[/]",
               inspection.FAIL: "[red]something is broken — fix the ✗ first[/]"}
    ctx.console.print(f"  {verdict[doc.worst]}")
    return CommandResult(handled=True)


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


def _terse(args, ctx: CommandContext) -> CommandResult:
    """Toggle terse model output (B2). Default ON; cuts wordy prose to save tokens."""
    ctx.session.terse = not ctx.session.terse
    if ctx.session.terse:
        ctx.console.print("terse: [green]ON[/] [dim](report only deltas; tool output "
                          "and errors are untouched — /terse to disable)[/]")
    else:
        ctx.console.print("terse: [yellow]OFF[/] [dim](full prose)[/]")
    return CommandResult(handled=True)


def _compact_mode(args, ctx: CommandContext) -> CommandResult:
    """Toggle compact display (WS4): tightens the on-screen output ceiling for the
    model's final answer. Independent of /verbose (full) and /terse (model prose)."""
    ctx.session.compact = not ctx.session.compact
    if ctx.session.compact:
        ctx.console.print("compact: [yellow]ON[/] [dim](tighter on-screen output "
                          "ceiling; /verbose full or /debug for everything)[/]")
    else:
        ctx.console.print("compact: [green]OFF[/] [dim](default truncated output)[/]")
    return CommandResult(handled=True)


def _plan(args, ctx: CommandContext) -> CommandResult:
    """Plan mode (B5): /plan <objective> drafts a plan read-only, then the REPL
    asks whether to save it, execute it, or both."""
    objective = " ".join(args).strip()
    if not objective:
        ctx.console.print("[yellow]Usage: /plan <objective>[/]")
        return CommandResult(handled=True)
    ctx.session.plan_pending = objective
    ctx.console.print(f"[green]✓[/] planning [dim](read-only draft; you'll choose "
                      f"save / execute / both next)[/]\n  [dim]{objective}[/]")
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
                ctx.console.print(f"  [cyan]{a['path']}[/] "
                                  f"[dim]{a['size']} bytes[/]{mark}")
        else:
            ctx.console.print("[yellow]Usage: /attach <path> [...][/]")
        return CommandResult(handled=True)

    total = sum(len(a["content"]) for a in ctx.session.attachments)
    for raw in args:
        p = Path(os.path.expanduser(raw))
        if not p.is_file():
            ctx.console.print(f"[yellow]✗ {raw}: no such file[/]")
            continue
        try:
            data = p.read_bytes()
        except OSError as e:
            ctx.console.print(f"[yellow]✗ {raw}: {e}[/]")
            continue
        if b"\0" in data[:_BINARY_SNIFF_BYTES]:
            ctx.console.print(f"[yellow]✗ {raw}: looks binary — refused[/]")
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
                f"[yellow]✗ {raw}: skipped — {ATTACH_MAX_TOTAL_BYTES // 1024}KB "
                f"total attachment cap reached[/]")
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
        ctx.console.print(f"[green]✓[/] attached [cyan]{p}[/] "
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


def _gitaudit(args, ctx: CommandContext) -> CommandResult:
    return _git_analysis("gitaudit", ctx, _parse_deep(args))


def _gitchange(args, ctx: CommandContext) -> CommandResult:
    return _git_analysis("gitchange", ctx, _parse_deep(args))


def _resume(args, ctx: CommandContext) -> CommandResult:
    if ctx.on_resume is None:
        ctx.console.print("[yellow]resume unavailable.[/]")
        return CommandResult(handled=True)
    ctx.on_resume(args[0] if args else "")
    return CommandResult(handled=True)


def _clear(args, ctx: CommandContext) -> CommandResult:
    """Start a fresh conversation: drop the turns AND the per-turn status.

    The status bar's `ctx N%` / `cache` describe the conversation that just went
    away; leaving them up made a cleared session look full (reported 2026-07-30).
    The window size and mode flags survive — those are settings, not history.
    """
    ctx.session.turns.clear()
    ctx.session.pinned_slot = None
    ctx.session.attachments.clear()
    reset_turn_status(ctx.status)
    ctx.console.print("[dim]· conversation cleared[/]")
    return CommandResult(handled=True)


def reset_turn_status(status) -> None:
    """Zero the turn-derived status fields (context pressure, resident prompt
    size, timings, step count). Keeps slot/model/num_ctx: they still apply."""
    if status is None:
        return
    for field, value in (("ctx_pressure", 0.0), ("prompt_tokens", 0),
                         ("steps", 0), ("wall_s", 0.0), ("tok_per_s", 0.0),
                         ("has_turn", False)):
        if hasattr(status, field):
            setattr(status, field, value)


def _quit(args, ctx: CommandContext) -> CommandResult:
    return CommandResult(handled=True, exit=True)
