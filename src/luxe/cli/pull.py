"""`luxe pull` — model weights into the local oMLX store (mount first, else
HuggingFace through oMLX's own downloader)."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from luxe.cli._common import _chat_cfg, _omlx_base_url_from_config, console, main


@main.command(name="pull")
@click.argument("ref", required=False, default="")
@click.option("--search", "search_query", default="",
              help="Search HuggingFace for MLX models instead of downloading.")
@click.option("--list", "list_state", is_flag=True,
              help="Show local models and any in-flight downloads.")
@click.option("--from", "from_path", default="",
              help="Import from an explicit directory (a mounted volume, an export).")
@click.option("--hf", "force_hf", is_flag=True,
              help="Skip the mount scan and fetch from HuggingFace.")
@click.option("--remove", "remove_state", is_flag=True,
              help="Delete <ref> from the LOCAL store instead of fetching. "
                   "Refuses this host's manifest models unless --force.")
@click.option("--force", is_flag=True, help="Replace an existing model directory.")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Don't ask to confirm.")
@click.option("--base-url", default="", help="oMLX endpoint (default: local).")
@click.option("--models-dir", default="",
              help="oMLX model store (default: ~/.omlx/models).")
def pull_cmd(ref: str, search_query: str, list_state: bool, from_path: str,
             force_hf: bool, remove_state: bool, force: bool, assume_yes: bool,
             base_url: str, models_dir: str):
    """Fetch model weights: `luxe pull <hf-repo-id>` or `luxe pull <name> --from <dir>`.

    Prefers a copy already on a mounted volume (kappa/alpha over SMB) — same
    bytes at LAN speed — and falls back to HuggingFace via the oMLX downloader.
    """
    from luxe import modelstore as ms

    endpoint = base_url or _omlx_base_url_from_config()
    dest_dir = Path(models_dir) if models_dir else ms.DEFAULT_MODELS_DIR
    # Only the CONFIG can tell us the engine; an explicit --base-url names an
    # endpoint whose stack luxe was never told, so keep the old assumption.
    engine = "omlx" if base_url else _default_engine_from_config()

    if remove_state:
        _pull_remove(ref, dest_dir, force=force, assume_yes=assume_yes)
        return

    # `--list` and `--remove` are local-store reads and stay available
    # everywhere; everything past here needs oMLX's admin API.
    if not list_state:
        _refuse_pull_on_non_omlx(engine, verb="fetch weights")

    with ms.OmlxAdmin(base_url=endpoint) as admin:
        try:
            if search_query:
                _pull_search(admin, search_query)
                return
            if list_state:
                _pull_list(admin, dest_dir, endpoint=endpoint,
                           base_url_given=bool(base_url), engine=engine)
                return
            if not ref:
                console.print("[yellow]Nothing to do — pass a model "
                              "(`luxe pull mlx-community/Qwen3.6-27B-6bit`), "
                              "`--search <query>`, or `--list`.[/]")
                sys.exit(2)

            name = ms.store_name_for(ref)
            # Store state FIRST — before a ≤20s mount scan, and keyed on
            # whether the weights RESOLVE, not on the name being listed. A
            # dangling entry (the HF-cache-wipe signature) used to hit
            # "already in the store — pass --force", so the fix `/doctor`
            # printed for it did nothing. A broken entry is replaceable.
            state = ms.model_state(name, dest_dir)
            if state == "ok" and not force:
                console.print(f"[yellow]· {name} is already in {dest_dir} "
                              "— pass --force to replace it.[/]")
                sys.exit(0)
            source_ref = ref
            if state not in ("missing", "ok"):
                console.print(f"[dim]· {name} is in the store but its "
                              f"weights don't resolve ({state}) — replacing "
                              "it[/]")
                # A bare name can only be found on a mount; the dangling
                # link itself names the HF repo it pointed at, so offer that
                # too instead of "an HF fetch needs a full repo id".
                if "/" not in ref and not from_path:
                    guessed = ms.hf_repo_for(name, dest_dir)
                    if guessed:
                        source_ref = guessed
            if not from_path and not force_hf:
                console.print("[dim]· scanning mounted volumes…[/]")
            sources = ms.resolve_pull_sources(
                source_ref, admin=admin, from_path=from_path,
                include_mounts=not force_hf)
            if not sources:
                # With --from the only empty case is "not a model directory";
                # without it, nothing anywhere has these weights.
                if from_path:
                    console.print(f"[red]✗ {from_path} is not an MLX model "
                                  "directory (needs config.json + weights).[/]")
                else:
                    console.print(
                        f"[red]✗ Nowhere to pull {ref!r} from.[/] Not on a mounted "
                        "volume, and an HF fetch needs a full repo id "
                        "(`org/Model`). Try `luxe pull --search <query>`.")
                sys.exit(2)

            chosen = sources[0]
            console.print(f"[bold]{name}[/] ← {chosen.describe()}")
            if len(sources) > 1:
                for alt in sources[1:]:
                    console.print(f"  [dim]alt: {alt.describe()}[/]")
            if not assume_yes and not click.confirm("Pull it?", default=True):
                console.print("[dim]· cancelled[/]")
                return

            if chosen.kind == "mount":
                _pull_from_mount(chosen, dest_dir, force)
            else:
                _pull_from_hf(admin, chosen)
        except (ms.ModelStoreError, OSError) as e:
            # OSError too: a full disk, a vanished mount, or a permission
            # error mid-copy is an operator-facing failure, not a traceback.
            console.print(f"[red]✗ {e}[/]")
            sys.exit(4)
        except KeyboardInterrupt:
            console.print("\n[yellow]· interrupted (partial copy removed)[/]")
            sys.exit(130)


def _default_engine_from_config() -> str:
    """Engine of the config's DEFAULT backend entry (`omlx` when unknown)."""
    from luxe.config import ENGINE_OMLX
    try:
        cfg = _chat_cfg()
        entry = cfg.backend_entry(cfg.default_backend_name())
        return getattr(entry, "engine", ENGINE_OMLX) or ENGINE_OMLX
    except Exception:
        return ENGINE_OMLX


def _refuse_pull_on_non_omlx(engine: str, *, verb: str) -> None:
    """Stop a `luxe pull` subcommand that has no meaning off oMLX.

    `luxe pull` is built on oMLX's admin API (`/admin/api/login`, `/admin/api/hf/*`)
    and on the `~/.omlx/models` store. Against llama-server every one of those
    calls 404s, and the failure that surfaces is a confusing
    "oMLX admin login failed: 404" rather than the true answer, which is that
    this host provisions weights a different way. Say so, and point at it.
    """
    from luxe.config import ENGINE_OMLX, ENGINE_OPENROUTER
    if engine == ENGINE_OMLX:
        return
    console.print(
        f"[red]✗ `luxe pull` cannot {verb} on a {engine} endpoint.[/] "
        f"It drives oMLX's admin API and the ~/.omlx/models store, neither of "
        f"which {engine} has.")
    if engine == ENGINE_OPENROUTER:
        # Nothing is downloadable here at all: the provider hosts the weights
        # and bills per token. Pointing at a preset file would be nonsense.
        console.print(
            "[dim]  OpenRouter hosts the weights and bills per token — there "
            "is nothing to fetch onto this disk. Pick a model with "
            "`/model find <text>` then `/model all <id>` inside "
            "`luxe chat --backend openrouter`.[/]\n"
            "[dim]  `luxe pull --list` and `--remove` still work — they only "
            "read the local store.[/]")
    else:
        console.print(
            "[dim]  This host serves GGUF weights named in its llama-server "
            "preset (neo: `~/dotfiles/luxe/neo-models.ini`). Fetch the file "
            "yourself, put it where the preset points, and restart the "
            "server.[/]\n"
            "[dim]  `luxe pull --list` and `--remove` still work — they only "
            "read the local store.[/]")
    sys.exit(2)


def _pull_search(admin, query: str) -> None:
    from luxe.modelstore import human_bytes

    hits = admin.search(query)
    if not hits:
        console.print(f"[yellow]No MLX models found for {query!r}.[/]")
        return
    console.print(f"[bold]HuggingFace — MLX models matching {query!r}[/]")
    for m in hits:
        size = f"  [dim]{human_bytes(m.size_bytes)}[/]" if m.size_bytes else ""
        console.print(f"  {m.repo_id}{size}  [dim]↓{m.downloads:,}[/]")
    console.print("[dim]· `luxe pull <repo-id>` to fetch one[/]")


def _pull_remove(ref: str, dest_dir, *, force: bool, assume_yes: bool) -> None:
    """`luxe pull <name> --remove`: delete one entry from the local store.

    Manifest-guarded: this host's declared main/fallback/keep models are the
    fallback kit — deleting one is refused without --force.
    """
    from luxe import modelstore as ms

    if not ref:
        console.print("[red]✗ --remove needs a model name "
                      "(`luxe pull <name> --remove`).[/]")
        sys.exit(2)
    name = ms.store_name_for(ref)
    try:
        manifest = _chat_cfg().host_manifest()
    except Exception:
        manifest = None
    if manifest is not None and name in manifest.all_models() and not force:
        console.print(f"[red]✗ {name} is in this host's manifest "
                      "(configs/chat.yaml hosts:) — it's part of the fallback "
                      "kit. Pass --force if you really mean it.[/]")
        sys.exit(2)
    state = ms.model_state(name, dest_dir)
    if state == "missing":
        console.print(f"[yellow]· {name} is not in {dest_dir} — nothing to do.[/]")
        sys.exit(0)
    if not assume_yes and not click.confirm(
            f"Remove {name} ({state}) from {dest_dir}?", default=False):
        console.print("[dim]· cancelled[/]")
        return
    try:
        freed, note = ms.remove_model(name, dest_dir)
    except ms.ModelStoreError as e:
        console.print(f"[red]✗ {e}[/]")
        sys.exit(4)
    detail = f" · {ms.human_bytes(freed)} freed" if freed else ""
    console.print(f"[bold]✓ {name}[/] — {note}{detail}")


def _pull_list(admin, dest_dir, *, endpoint: str = "",
               base_url_given: bool = False, engine: str = "omlx") -> None:
    from luxe.modelstore import (ModelStoreError, human_bytes,
                                 local_model_names, model_state)

    # `--base-url <remote>` used to silently list the LOCAL store (2026-07-30
    # finding) — a remote endpoint's disk is only knowable via its admin API.
    remote = False
    if base_url_given:
        try:
            from luxe.chat.origin import endpoint_is_local
            remote = not endpoint_is_local(endpoint)
        except Exception:
            remote = False
    if remote:
        try:
            stored = admin.stored_models()
        except ModelStoreError as e:
            console.print(f"[red]✗ can't list {endpoint}'s store: {e}[/]")
            return
        console.print(f"[bold]Models on {endpoint}[/]")
        for m in stored:
            size = m.get("size_bytes") or m.get("size") or 0
            suffix = f"  [dim]{human_bytes(size)}[/]" if size else ""
            console.print(f"  · {m.get('name') or m.get('repo_id')}{suffix}")
        if not stored:
            console.print("  [dim](none reported)[/]")
        return

    names = local_model_names(dest_dir)
    console.print(f"[bold]Local models[/] [dim]({dest_dir})[/]")
    for n in names:
        state = model_state(n, dest_dir)
        if state == "ok":
            console.print(f"  · {n}")
        else:
            # A listed model the server can't load is worse than an absent
            # one — say so instead of letting the stub masquerade as weights.
            console.print(f"  · {n}  [red]⚠ {state}[/] "
                          f"[dim](weights don't resolve — `luxe pull` it "
                          f"again or `--remove` the stub)[/]")
    if not names:
        console.print("  [dim](none)[/]")
    from luxe.config import ENGINE_OMLX
    if engine != ENGINE_OMLX:
        # There is no download queue to report: this endpoint has no admin
        # API and luxe never fetches for it. Asking anyway printed
        # "download queue unavailable: no oMLX API key", which reads as a
        # broken key on a host that needs none.
        console.print(f"[dim]· {engine} has no download queue — weights come "
                      "from its preset file[/]")
        return
    try:
        tasks = admin.tasks()
    except ModelStoreError as e:
        console.print(f"[dim]· download queue unavailable: {e}[/]")
        return
    if tasks:
        console.print("[bold]Downloads[/]")
        for t in tasks:
            console.print(f"  · {t.repo_id} — {t.status} {t.progress:.0f}% "
                          f"[dim]{human_bytes(t.downloaded_size)}"
                          f"/{human_bytes(t.total_size)}[/]"
                          + (f" [red]{t.error}[/]" if t.error else ""))


def _pull_from_mount(source, dest_dir, force: bool) -> None:
    from rich.progress import (BarColumn, DownloadColumn, Progress,
                               TextColumn, TimeRemainingColumn)

    from luxe import modelstore as ms

    with Progress(TextColumn("[dim]copying[/]"), BarColumn(), DownloadColumn(),
                  TimeRemainingColumn(), console=console) as bar:
        task = bar.add_task("copy", total=source.size_bytes or None)
        res = ms.copy_into_store(
            source, models_dir=dest_dir, force=force,
            on_progress=lambda done, total: bar.update(task, completed=done),
        )
    console.print(f"[green]✓[/] {res.name} → {res.dest} "
                  f"[dim]({ms.human_bytes(res.bytes_copied)} in {res.seconds:.0f}s)[/]")
    console.print("[dim]· oMLX picks it up on its next model scan "
                  "(`luxe pull --list` to confirm)[/]")


def _pull_from_hf(admin, source) -> None:
    from rich.progress import (BarColumn, DownloadColumn, Progress,
                               TextColumn, TimeRemainingColumn)

    task_rec = admin.start_download(source.ref)
    console.print(f"[dim]· oMLX download task {task_rec.task_id}[/]")
    with Progress(TextColumn("[dim]downloading[/]"), BarColumn(), DownloadColumn(),
                  TimeRemainingColumn(), console=console) as bar:
        row = bar.add_task("dl", total=task_rec.total_size or None)

        def _tick(t):
            bar.update(row, completed=t.downloaded_size,
                       total=t.total_size or None)

        final = admin.wait_for(task_rec.task_id, on_progress=_tick)
    if final.status == "completed":
        console.print(f"[green]✓[/] {final.repo_id} downloaded")
        # Recent oMLX downloads land as REAL bytes nested in the store
        # (`<store>/<org>/<name>`); older ones left only an HF-cache copy —
        # the wipe-vulnerable state. Materialize only when needed.
        from luxe.modelstore import model_state
        if model_state(source.name) == "ok":
            console.print(f"[green]✓[/] {source.name} — real bytes in the store")
        else:
            _materialize_from_hf_cache(source)
    elif final.status == "cancelled":
        console.print(f"[yellow]· {final.repo_id} download cancelled[/]")
    else:
        console.print(f"[red]✗ {final.repo_id} failed: "
                      f"{final.error or final.status}[/]")
        sys.exit(4)


def _materialize_from_hf_cache(source, models_dir=None) -> None:
    """Copy a just-downloaded HF model out of the cache into the oMLX store as
    REAL bytes (2026-07-30). oMLX's downloader leaves weights only in
    `~/.cache/huggingface` — the cache that has already been wiped once. A
    fallback-kit model must survive cache eviction, so the store entry is a
    dereferenced copy, not a symlink. Best-effort: a failed copy leaves the
    cache download intact and says so."""
    from luxe import modelstore as ms

    try:
        cache_dir = ms.hf_cache_dir_for(source.ref)
        snap = ms._resolve_hf_snapshot(cache_dir)
        if snap is None:
            console.print("[yellow]· downloaded, but no loadable snapshot "
                          f"found under {cache_dir} — store not updated[/]")
            return
        src = ms.ModelSource(kind="mount", ref=str(snap), name=source.name,
                             size_bytes=ms.dir_size(snap), note="hf-cache")
        console.print(f"[dim]· materializing into the store "
                      f"({ms.human_bytes(src.size_bytes)} — cache copies "
                      "don't survive eviction)[/]")
        ms.copy_into_store(src, models_dir=models_dir, force=True)
        console.print(f"[green]✓[/] {source.name} → real bytes in the store")
    except Exception as e:
        # Name the model's OWN cache directory: `--from` wants a model dir
        # (or its `models--org--Name` parent), and the hub root is neither.
        console.print(f"[yellow]· store materialization failed ({e}) — the "
                      f"model is only in the HF cache; re-run "
                      f"`luxe pull {source.name} --from "
                      f"{ms.hf_cache_dir_for(source.ref)}` to fix[/]")
