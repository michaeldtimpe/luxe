"""Model, backend, and weight-store commands: `/model` `/backend` `/pull` `/unload`.

Split out of `commands.py` 2026-08-04 (behavior unchanged). The dispatcher in
`commands._build_handlers` is the only importer.
"""

from __future__ import annotations

from luxe.chat import modelcaps
from luxe.chat import origin as origin_mod
from luxe.chat.commands import _SLOTS, CommandContext, CommandResult


def _warn_model_not_offered(ctx: CommandContext, model_id: str) -> None:
    """Explain an explicit `/model <slot> <id>` the picker didn't list.

    Three states, three different unlocks: hidden by the `visible_models:`
    roster (allowed — this is how the m5 capacity model is selected), served
    by nothing here (needs a pull), or simply unverifiable because oMLX is
    down. Silence used to defer all three to a swap failure a turn later.
    Never raises — this is advisory only, the override is set regardless.
    """
    try:
        offered = ctx.slots.available_models()
        if not offered or model_id in offered:
            return
        served = set(ctx.slots.backend.list_models())
    except Exception:
        return
    if model_id in served:
        ctx.console.print(
            f"  [dim]· {model_id} is served here but hidden from the picker "
            "by `visible_models:` in configs/chat.yaml — using it anyway[/]")
    else:
        # `/pull` is meaningless where the provider hosts the weights: point at
        # the catalog search instead, which is how you find a real id there.
        try:
            hosted = ctx.slots.active_entry().is_openrouter()
        except Exception:
            hosted = False
        fix = ("`/model find <text>` for the exact id"
               if hosted else f"`/pull {model_id} --yes` to fetch it")
        ctx.console.print(
            f"  [yellow]· {model_id} isn't in this endpoint's catalog[/] "
            f"[dim]— {fix}, or `/model` for the list. The next turn will fail "
            "until it resolves.[/]")


def _model(args, ctx: CommandContext) -> CommandResult:
    """Show/repoint the chat|plan|code model slots.

    `/model`                list slots + a numbered list of available oMLX models
    `/model <slot>`         show that slot's model
    `/model <slot> <n>`     point the slot at the n-th available model
    `/model <slot> <id>`    point the slot at an explicit model id
    `/model all <n|id>`     point ALL slots (chat+plan+code) at one model
    `/model find <text>`    search the endpoint's FULL catalog (id + price)

    `all` exists because freeform turns are keyword-routed to a slot
    (`_infer_task_type`), so pinning only `chat` still lets a "fix…"/"add…"
    message land on the code slot's model. One command = the whole session
    runs the picked model.
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
            ctx.console.print(f"[dim]({ctx.slots.engine_label()} unreachable — "
                              "`/model <slot> <id>` still works)[/]")
        if ctx.slots.catalog_is_larger_than_roster():
            ctx.console.print("[dim]· `/model find <text>` searches the full "
                              "catalog (this list is the shortlist)[/]")
        return CommandResult(handled=True)
    slot = args[0]
    # `find` is a SEARCH, not a slot: it reads the catalog and selects nothing.
    if slot == "find":
        return _model_find(args[1:], ctx)
    if slot not in _SLOTS and slot != "all":
        ctx.console.print(f"[yellow]Unknown slot {slot!r}; expected "
                          "chat|plan|code|all (or `find <text>`).[/]")
        return CommandResult(handled=True)
    if len(args) < 2:
        for s in (_SLOTS if slot == "all" else (slot,)):
            ctx.console.print(f"  {s} → {ctx.slots.model_for(s)}")
        return CommandResult(handled=True)
    sel = args[1]
    # Numeric selection indexes into the available-model list (1-based).
    if sel.isdigit():
        avail = ctx.slots.available_models()
        idx = int(sel)
        if not avail:
            ctx.console.print("[yellow]No available-model list "
                              f"({ctx.slots.engine_label()} unreachable) "
                              "— pass an explicit id: /model <slot> <id>.[/]")
            return CommandResult(handled=True)
        if not (1 <= idx <= len(avail)):
            ctx.console.print(f"[yellow]Pick 1–{len(avail)} (see /model).[/]")
            return CommandResult(handled=True)
        model_id = avail[idx - 1]
    else:
        model_id = sel
    # An explicit id the picker doesn't offer has TWO very different causes,
    # and "unknown model" would be wrong for one of them: the server may well
    # serve it while `visible_models:` hides it from the roster (a supported
    # thing to ask for — the m5 capacity model is reached exactly this way).
    # Say which, and name the unlock, instead of failing at swap time.
    if not sel.isdigit():
        _warn_model_not_offered(ctx, model_id)

    targets = tuple(_SLOTS) if slot == "all" else (slot,)
    for s in targets:
        ctx.slots.set_override(s, model_id)
    if slot == "all":
        ctx.console.print(f"[green]✓[/] slots [cyan]{'·'.join(targets)}[/] → "
                          f"{model_id} [dim](swaps on next turn)[/]")
    else:
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


_FIND_MAX_ROWS = 30


def _price_per_1m(raw) -> str:
    """A `pricing` value (USD PER TOKEN, as a string) rendered per 1M tokens.

    Catalogs quote per-token figures like "0.0000006", which is unreadable and
    incomparable at a glance; every published price list is per-million. "-"
    when the field is missing or unparseable — an invented number here is a
    number someone budgets against.
    """
    try:
        per_1m = float(raw) * 1_000_000
    except (TypeError, ValueError):
        return "-"
    if per_1m <= 0:
        return "free"
    if per_1m < 1:
        return f"${per_1m:.2f}"
    if per_1m < 100:
        return f"${per_1m:.2f}"
    return f"${per_1m:,.0f}"


def _model_find(args, ctx: CommandContext) -> CommandResult:
    """`/model find <text>` — case-insensitive substring search over the FULL
    live catalog, with per-1M-token prices.

    The roster `/model` prints is a shortlist by design (a cloud catalog is
    ~300 entries and the config cannot enumerate it). This is how you discover
    the exact id to pass to `/model all <id>`, which bypasses the roster.
    """
    query = " ".join(args).strip().lower()
    if not query:
        ctx.console.print("[yellow]Usage: /model find <text>[/]")
        return CommandResult(handled=True)
    records = ctx.slots.catalog()
    if not records:
        ctx.console.print(f"[yellow]No catalog from {ctx.slots.engine_label()} "
                          "— endpoint unreachable, or it reports no models.[/]")
        return CommandResult(handled=True)

    hits = sorted(
        (r for r in records if query in str(r.get("id", "")).lower()),
        key=lambda r: str(r.get("id", "")),
    )
    if not hits:
        ctx.console.print(f"[yellow]No model id contains {query!r}[/] "
                          f"[dim]({len(records)} in this catalog)[/]")
        return CommandResult(handled=True)

    ctx.console.print(f"[bold]{len(hits)} match{'es' if len(hits) != 1 else ''}[/] "
                      f"[dim]for {query!r} — prices per 1M tokens (in/out)[/]")
    for rec in hits[:_FIND_MAX_ROWS]:
        mid = str(rec.get("id", ""))
        pricing = rec.get("pricing") if isinstance(rec.get("pricing"), dict) else {}
        price = ""
        if pricing:
            price = (f"  [dim]{_price_per_1m(pricing.get('prompt'))} / "
                     f"{_price_per_1m(pricing.get('completion'))}[/]")
        # A model that cannot call tools is a conversation-only model here —
        # the same thing modelcaps says about gemma locally, read from the
        # catalog instead of from a chat template.
        params = rec.get("supported_parameters")
        no_tools = (isinstance(params, list) and params
                    and "tools" not in params)
        warn = "  [yellow]⚠ no tool support[/]" if no_tools else ""
        ctx.console.print(f"  {mid}{price}{warn}")
    if len(hits) > _FIND_MAX_ROWS:
        ctx.console.print(f"[dim]  … {len(hits) - _FIND_MAX_ROWS} more, "
                          "refine your search[/]")
    ctx.console.print("[dim]· `/model all <id>` to run the whole session on "
                      "one of these[/]")
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
        # Name the unlock: an unreachable REMOTE entry is almost always the
        # link or the key, and both are one command away.
        entry = entries[name]
        key_env = getattr(entry, "api_key_env", "") or "OMLX_API_KEY"
        billable = False
        try:
            billable = entry.is_billable()
        except Exception:
            billable = False
        if billable:
            # A cloud endpoint has no tunnel and no local process. The two real
            # causes are the key and the account balance; `/planeproxy` advice
            # here would send someone debugging a link that is fine.
            ctx.console.print(
                f"  [dim]→ needs ${key_env} set (env or "
                f"`~/.luxe/secrets.env`); if the key is good, `/usage` shows "
                "whether the account still has credits[/]")
        else:
            ctx.console.print(f"  [dim]→ `/net` to diagnose the link; a remote "
                              f"entry also needs ${key_env} set (and "
                              "`/planeproxy` up, if it rides the tunnel)[/]")
        return CommandResult(handled=True)
    entry = entries[name]
    ctx.console.print(f"[green]✓[/] backend → [cyan]{name}[/] "
                      f"[dim]({entry.base_url}; timeout {entry.timeout_s:.0f}s)[/]")
    # Say when the entry repointed the slots. A backend switch that silently
    # changes which model answers is the same failure shape auto-degrade
    # exists to prevent — announce it, and name the way to override it.
    applied = getattr(ctx.slots, "default_model_applied", "")
    if applied:
        ctx.console.print(f"  [dim]· slots → {applied} "
                          f"(this backend's `default_model:`; "
                          f"`/model all <id>` to pick another)[/]")
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
            if not from_path:
                ctx.console.print("[dim]· looking for it (mounts, then HF)…[/]")
            sources = ms.resolve_pull_sources(
                ref, admin=admin, from_path=from_path,
                include_mounts="--hf" not in flags)
            if not sources:
                # With --from the only empty case is "not a model directory";
                # without it, nothing anywhere has these weights.
                if from_path:
                    ctx.console.print(f"[red]✗ {from_path} is not an MLX model "
                                      "directory (config.json + weights).[/]")
                else:
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
