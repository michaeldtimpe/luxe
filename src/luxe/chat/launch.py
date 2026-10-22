"""Startup path for `luxe chat` / `luxe code`: resolve → index → lock → run.

Lifted verbatim out of `cli.py` 2026-08-04. `cli.chat_cmd` / `cli.code_cmd`
are thin Click shells over `_run_interactive`; the option list they share lives
here too, so the two postures cannot drift apart.

The handful of names this reaches back into `cli` for (`_chat_cfg`,
`_default_mcp_config_hint`, `_languages_from_paths`, `_select_backend`)
are imported INSIDE `_run_interactive`. `cli` imports this module at module
level (from `cli/chat.py`), so a top-level import back would be circular — and
the late binding reads the `luxe.cli` package attributes at call time. Since
the 2026-09-26 package split those are re-exports of `luxe.cli._common`, so
`_chat_cfg`'s own lookup of `_default_chat_config` is patched at
`luxe.cli._common`.
(Repo resolution now comes from `luxe.gitclone.resolve_repo` at module level
— moved out of `cli` 2026-08-05, deferred-list #6.)
"""

from __future__ import annotations

import os
import sys
import time

import click
from rich.console import Console
from rich.markup import escape

from luxe import ephemeral as eph
from luxe import spec_resolver
from luxe import textfmt
from luxe.gitclone import resolve_repo

# Own Console instance (cli has its own). Rich resolves `sys.stdout` at write
# time, so CliRunner capture works the same through either.
console = Console()


# Chat startup index bounds. The root is user-chosen, and `luxe chat --repo ~`
# means ~1M files / 56k source candidates: 210s of tokenizing and tree-sitter
# parsing before the first prompt (measured 2026-07-30). A project directory is
# nowhere near these caps, so they only bite when you point luxe at a home
# directory — and when they do, startup SAYS so.
_INDEX_MAX_FILES = int(os.environ.get("LUXE_INDEX_MAX_FILES", "8000"))
_INDEX_MAX_MB = int(os.environ.get("LUXE_INDEX_MAX_MB", "96"))


def _build_chat_indexes(repo_path: str):
    """Build the BM25 + symbol indexes from ONE bounded scan, with progress.

    Chat-only: `luxe maintain` and the benchmarks keep calling the builders
    directly (unbounded walk, byte-identical). Returns (bm25, symbol_index, scan)
    — the scan is reused for language detection instead of walking again.
    """
    from luxe import fswalk
    from luxe import search as search_mod
    from luxe import symbols as symbols_mod

    extensions = frozenset(search_mod._DEFAULT_EXTENSIONS) | frozenset(
        ext for exts in symbols_mod._LANGUAGE_EXTENSIONS.values() for ext in exts)

    t0 = time.time()
    with console.status("[dim]· Indexing repository for search "
                        "(model loads on first turn)…[/]") as status:
        scan = fswalk.scan_source_files(
            repo_path, extensions=extensions,
            max_files=_INDEX_MAX_FILES,
            max_total_bytes=_INDEX_MAX_MB * 1024 * 1024,
            use_git=os.environ.get("LUXE_INDEX_NO_GIT") != "1",
            on_progress=lambda n: status.update(
                f"[dim]· Scanning… {n} files[/]"),
        )
        status.update(f"[dim]· Indexing {scan.count} files (bm25)…[/]")
        bm25 = search_mod.build_bm25_index(repo_path, files=scan.paths)
        sym_idx = symbols_mod.build_symbol_index(
            repo_path, files=scan.paths,
            on_progress=lambda n: status.update(
                f"[dim]· Indexing symbols… {n}/{scan.count} files[/]"),
        )

    how = "git-tracked" if scan.used_git else "walked"
    console.print(f"[dim]  indexed {len(bm25.paths)} files · "
                  f"{len(sym_idx.symbols)} symbols · {how} · "
                  f"{time.time() - t0:.1f}s[/]")
    if scan.truncated:
        # Never a silent cap: name the limit, the consequence, and both escapes.
        console.print(
            f"[yellow]· index truncated at the {scan.truncated}[/] [dim]— "
            f"`bm25_search`/`find_symbol` see the {scan.count} shallowest files "
            f"under {repo_path}, not the whole tree. Chat from a project "
            f"directory (git-tracked files only, ~1s), or raise "
            f"LUXE_INDEX_MAX_FILES / LUXE_INDEX_MAX_MB.[/]")
    return bm25, sym_idx, scan


# Home-relative display path (`~/Downloads/luxe`). Local name kept: `cli`
# re-exports `_tilde` and the startup messages below read better with it.
_tilde = textfmt.tilde


def _resolve_theme_name(flag: str | None) -> str:
    """Chat palette precedence: `--theme` → `LUXE_THEME` → persisted `/theme`
    choice (`~/.luxe/theme`) → `auto`.

    `auto` tracks the user's ACTIVE YASL/statusline theme (chat.sdd; the
    `--theme` help text says the same). The curated luxe palettes are OPT-IN —
    shipping `cool` as the default silently overrode the user's own theme
    (reported 2026-07-29). `/theme <name>` persists (2026-07-30), so a chosen
    palette survives across sessions without a flag or env var.
    """
    explicit = flag or os.environ.get("LUXE_THEME")
    if explicit:
        return explicit
    try:
        from luxe.chat import theme as theme_mod
        return theme_mod.load_preference() or "auto"
    except Exception:
        return "auto"


def _shared_chat_options(f):
    """Click options shared by `luxe chat` and `luxe code`. One list, two
    commands: the postures differ (project requirement, write default), the
    option surface must not drift."""
    opts = [
        click.option("--repo", "repo", default=".",
                     help="Where to work (default: cwd). Resolves UP to the "
                          "enclosing git root / project; a directory that is "
                          "neither starts an unindexed chat session."),
        click.option("--config", "config_path", default=None,
                     help="Path to config YAML (default: configs/chat.yaml)"),
        click.option("--resume", "resume_session_id", default=None,
                     help="Resume a prior chat session by id"),
        click.option("--backend", "backend_name", default=None,
                     help="Start on this configured backend (a `backends:` "
                          "entry name, e.g. m5). Chat-only; default = the "
                          "entry marked default. Includes `openrouter`, a "
                          "cloud, metered, opt-in backend with a hard "
                          "session budget; its `default_model:` "
                          "(moonshotai/kimi-k3) applies only when you don't "
                          "set one."),
        click.option("--chat-model", default=None,
                     help="Override the chat-slot model"),
        click.option("--plan-model", default=None,
                     help="Override the plan-slot model"),
        click.option("--code-model", default=None,
                     help="Override the code-slot model"),
        click.option("--keep-loaded", is_flag=True, default=False,
                     help="Skip the post-session model unload."),
        click.option("--dev", "dev_mode", is_flag=True, default=False,
                     help="Start in dev mode: write tools + unrestricted shell "
                          "ON (equivalent to /write + /bash)."),
        click.option("--web", "start_web", is_flag=True, default=False,
                     help="Start with web tools ON (equivalent to /web): "
                          "web_fetch, plus web_search / web_answer when "
                          "their provider keys are configured, and "
                          "web_page (interactive open/click/type) when "
                          "Chromium is installed."),
        click.option("--verbose", "startup_verbose", default=None,
                     type=click.Choice(["off", "diff", "full"]),
                     help="Set tool-output verbosity at startup."),
        click.option("--show-reasoning", "startup_show_reasoning",
                     is_flag=True, default=False,
                     help="Stream the model's reasoning live from startup."),
        click.option("--no-terse", "startup_no_terse", is_flag=True,
                     default=False,
                     help="Disable terse model output (terse is ON by default)."),
        click.option("--debug", "startup_debug", is_flag=True, default=False,
                     help="Show everything: verbose full + reasoning."),
        click.option("--compact", "startup_compact", is_flag=True,
                     default=False,
                     help="Compact display: tighter on-screen output ceiling."),
        click.option("--theme", "theme_name", default=None,
                     help="Curated luxe color palette: auto|cool|warm|mono "
                          "(default: auto)."),
        click.option("--ctx", "ctx_tier", default=None,
                     type=click.Choice(["small", "medium", "large", "xlarge",
                                        "huge"]),
                     help="Start with this /ctx tier (clamped per-turn to the "
                          "box/model ceiling, same as /ctx)."),
        click.option("--mcp", "mcp_servers", multiple=True,
                     help="Connect this MCP server from the MCP config at "
                          "startup (repeatable). Chat-only; tools matching the "
                          "server's `gate_tools` need /write."),
        click.option("--mcp-config", "mcp_config_path", default=None,
                     help="Path to the MCP config YAML for --mcp "
                          "(default: configs/mcp.yaml)."),
        click.option("--mcp-read-only", is_flag=True, default=False,
                     help="Drop `gate_tools`-matching MCP tools entirely — "
                          "inspection surface only, even in write mode."),
        click.option("--ephemeral", "ephemeral", is_flag=True, default=False,
                     help="Leave nothing behind: no ~/.luxe/sessions/<id>/ "
                          "(transcript, debug.log), no run events, no writes "
                          "to the repo's .luxe/memory.md. Cannot be resumed "
                          "and has no post-hoc diagnostics. Does NOT affect "
                          "the write tools — /write still gates those."),
    ]
    for opt in reversed(opts):
        f = opt(f)
    return f


def _run_interactive(
    *, require_project: bool, start_write: bool,
    repo: str, config_path: str | None, resume_session_id: str | None,
    backend_name: str | None,
    chat_model: str | None, plan_model: str | None, code_model: str | None,
    keep_loaded: bool, dev_mode: bool, start_web: bool = False,
    startup_verbose: str | None, startup_show_reasoning: bool,
    startup_no_terse: bool, startup_debug: bool, startup_compact: bool,
    theme_name: str | None,
    ctx_tier: str | None = None,
    mcp_servers: tuple[str, ...] = (),
    mcp_config_path: str | None = None,
    mcp_read_only: bool = False,
    ephemeral: bool = False,
):
    """Shared body of `luxe chat` / `luxe code` (posture via the two kwargs)."""
    from luxe.chat import run_chat_repl
    from luxe.chat.controller import StartupFlags
    # Late, and from `cli` on purpose: see the module docstring.
    from luxe.cli import _chat_cfg, _select_backend
    from luxe.tools.fs import set_repo_root

    _enable_ephemeral(ephemeral, resume_session_id)
    theme_name = _resolve_theme_name(theme_name)

    repo_path = resolve_repo(repo)
    cfg = _chat_cfg(config_path)

    # CLI per-slot overrides become an ad-hoc model + slots block so the user
    # can point a slot at any oMLX-loadable model without editing YAML.
    _apply_slot_overrides(cfg, chat_model, plan_model, code_model)

    # `luxe code` runs under the host's code pair (HostManifest.code_main),
    # set before anything resolves a model.
    if require_project:
        cfg.apply_code_posture()

    # `--backend <name>` picks the startup endpoint by re-flagging the config's
    # default entry (chat-only; SlotManager reads default_backend_name()).
    _select_backend(cfg, backend_name)

    project = _resolve_project(repo_path, require_project=require_project)
    repo_path = project.root
    set_repo_root(repo_path)

    # Cache the `.sdd` contract scan per repo root for this session (chat-only;
    # see spec_resolver). `run_single` rebuilds the contract block every turn
    # and the scan walks the whole root — ~19s per turn with `--repo ~`.
    # `finalize_turn` drops the entry when a turn writes a `.sdd`.
    spec_resolver.enable_scan_cache()

    attached = _AttachedProject(repo_path)
    languages: frozenset[str] = frozenset()
    if project.is_project:
        languages, _stats = _index_project(repo_path)
        attached.lock()
    else:
        # No project here: skip indexing entirely (it cost 210s from `$HOME` for
        # coverage the model can't rely on). Read tools still work against cwd;
        # `/index` or `/project <path>` turns code search on later. No repo
        # lock either (see `_AttachedProject.lock`).
        console.print(
            f"[dim]· no project at {escape(_tilde(repo_path))} — chat mode "
            "(read tools on; `/project <path>` or `/index` to add code "
            "search)[/]")

    mcp_mgr = (_start_mcp(mcp_servers, mcp_config_path, read_only=mcp_read_only)
               if mcp_servers else None)
    run_app = _pick_front_end()
    flags = StartupFlags(
        keep_loaded=keep_loaded, resume_session_id=resume_session_id,
        project_kind=project.kind, theme_name=theme_name, dev_mode=dev_mode,
        start_web=start_web, start_write=start_write, verbose=startup_verbose,
        show_reasoning=startup_show_reasoning, no_terse=startup_no_terse,
        debug=startup_debug, compact=startup_compact, ctx_tier=ctx_tier)
    try:
        if run_app is not None:
            run_app(cfg, repo_path, languages, flags=flags,
                    on_project=attached.attach)
        else:
            run_chat_repl(cfg, repo_path, languages, console=console,
                          flags=flags, on_project=attached.attach)
    finally:
        _teardown(mcp_mgr, attached)


def _enable_ephemeral(ephemeral: bool, resume_session_id: str | None) -> None:
    """Ephemeral is switched on BEFORE anything can persist — the session
    directory is created during startup, so a later flip would already be
    too late. `--resume` needs a transcript on disk, which is precisely what
    this mode refuses to produce, so the combination is rejected rather than
    silently starting a fresh session under a resumed-looking id."""
    if not ephemeral:
        return
    if resume_session_id:
        raise click.UsageError(
            "--ephemeral cannot be combined with --resume: an ephemeral "
            "session writes no transcript, so there is nothing to resume "
            "and nothing this session could be resumed from.")
    eph.enable()


def _resolve_project(repo_path: str, *, require_project: bool):
    """What is this session about? A git root above cwd, a marker-bearing
    directory, or nothing at all (chat from anywhere).

    ALWAYS resolves upward, flag or not: the `luxe-chat` wrapper passes
    `--repo "$PWD"` on every invocation, so treating an explicit --repo as
    "pin exactly here" would silently defeat walk-up for the primary entry
    point (caught by tests/test_chat_project.py). `luxe code`
    (`require_project`) outside a project exits 2 with the fix."""
    from luxe import project as project_mod

    project = project_mod.resolve(repo_path)
    if require_project and not project.is_project:
        # `luxe code` is dead simple on purpose: inside a project it just
        # works; outside one it says so and stops rather than degrading.
        console.print(f"[red]✗ no project at {escape(_tilde(repo_path))} — "
                      "`luxe code` needs a git repo or a marker-bearing "
                      "directory.[/]")
        console.print("[dim]  cd into your project (or pass --repo <path>), "
                      "or use `luxe chat` for a no-project session.[/]")
        sys.exit(2)
    if project.root != repo_path and project.is_project:
        console.print(f"[dim]· {escape(_tilde(repo_path))} is inside "
                      f"{escape(_tilde(project.root))} — using the project root[/]")
    return project


class _AttachedProject:
    """The session's project root and the repo lock that goes with it.

    The repo lock serialises luxe runs against ONE codebase; a no-project
    session has no codebase to protect, and locking `$HOME` would stop you
    opening a second chat window — so only a real project is ever locked.
    `attach` is the front-ends' `on_project` hook (`/project`, `/index`)."""

    def __init__(self, root: str) -> None:
        self.root = root
        self._lock = None

    def lock(self) -> None:
        """Take the lock on `root` at startup; a busy repo exits 3."""
        from luxe.locks import LockHeld

        try:
            self._lock = _enter_lock(self.root)
        except LockHeld as e:
            console.print(f"[red]✗ {e}[/]")
            sys.exit(3)

    def attach(self, target: str | None) -> dict:
        """`/project [path]` and `/index [path]`: re-resolve, re-index, and move
        the repo lock. Returns a summary dict for the command to render; raises
        LockHeld when the new project is busy (the session stays where it is)."""
        from luxe import project as project_mod
        from luxe import search as search_mod
        from luxe import symbols as symbols_mod
        from luxe.tools.fs import set_repo_root

        new = project_mod.resolve(target if target else self.root)
        if new.is_project and new.root != self.root:
            # Acquire the new lock BEFORE releasing the old one, so a failure
            # leaves the session exactly as it was.
            new_lock = _enter_lock(new.root)
            self.release()
            self._lock = new_lock
        self.root = new.root
        set_repo_root(self.root)
        spec_resolver.invalidate_scan_cache()
        search_mod.reset_index()
        symbols_mod.reset_index()
        summary = {"root": new.root, "kind": new.kind, "label": new.label,
                   "files": 0, "symbols": 0, "truncated": "", "used_git": False}
        languages: frozenset = frozenset()
        if new.is_project:
            languages, stats = _index_project(new.root)
            summary.update(stats)
        # The front-ends' session follows this (controller.apply_project_summary):
        # turn setup reads `session.languages`, so lint/typecheck target the
        # project actually attached rather than the one the session started in.
        summary["languages"] = languages
        return summary

    def release(self) -> None:
        if self._lock is not None:
            try:
                self._lock.__exit__(None, None, None)
            except Exception:
                pass
            self._lock = None


def _enter_lock(root: str):
    from luxe.locks import acquire_repo_lock

    lock = acquire_repo_lock(root, f"chat-{int(time.time())}")
    lock.__enter__()
    return lock


def _pick_front_end():
    """The full-screen Textual TUI's entry point when stdout is a real
    terminal AND textual is installed; otherwise None = the line REPL (CI /
    pipes / textual-absent). --resume rides either front-end (the TUI
    replays the transcript on mount). chat.sdd."""
    if not sys.stdout.isatty():
        return None
    try:
        from luxe.chat.tui import run_chat_app
    except Exception:
        console.print("[dim]· textual not installed — using the line REPL. "
                      "Run `uv sync --extra chat` (repo root) to restore "
                      "the full-screen UI.[/]")
        return None
    return run_chat_app


def _teardown(mcp_mgr, attached: _AttachedProject) -> None:
    """Session over (or startup failed after the lock): drop the published
    indexes and the scan cache, close MCP, release the repo lock."""
    from luxe import search as search_mod
    from luxe import symbols as symbols_mod

    search_mod.reset_index()
    symbols_mod.reset_index()
    spec_resolver.enable_scan_cache(False)  # also clears it
    if mcp_mgr is not None:
        from luxe.chat import mcptools
        mcptools.clear()
        try:
            mcp_mgr.close()
        except Exception:
            pass
    attached.release()


def _index_project(root: str) -> tuple[frozenset, dict]:
    """Index `root` for this session (`_build_chat_indexes`), publish both
    indexes, and return `(languages, stats)` — the languages from the SAME
    scan (no third walk of the tree), the stats in `/project`'s summary
    shape. One copy for startup and `/project`/`/index`."""
    from luxe import search as search_mod
    from luxe import symbols as symbols_mod
    # Late, and from `cli` on purpose: see the module docstring.
    from luxe.cli import _languages_from_paths

    bm25, sym_idx, scan = _build_chat_indexes(root)
    search_mod.set_index(bm25)
    symbols_mod.set_index(sym_idx)
    return _languages_from_paths(scan.paths), {
        "files": len(bm25.paths), "symbols": len(sym_idx.symbols),
        "truncated": scan.truncated, "used_git": scan.used_git}


def _start_mcp(names: tuple[str, ...], config_path: str | None, *,
               read_only: bool):
    """`--mcp <name>` (chat-only): connect the named servers from the MCP
    config and publish their tool surface via chat.mcptools; prepare_turn
    injects it through the extra-tool seam. Returns the started manager (the
    caller closes it). An unknown name exits 2 with the configured list; a
    server that fails to connect is reported and skipped — an unreachable
    relay must not stop a chat session from starting (this is the fallback
    tool). `read_only` drops the `gate_tools`-matching tools entirely."""
    from luxe.chat import mcptools
    # Late, and from `cli` on purpose: see the module docstring.
    from luxe.cli import _default_mcp_config_hint
    from luxe.mcp.client import (
        MCPClientConfig,
        MCPClientManager,
        load_mcp_config,
    )
    mcp_cfg = load_mcp_config(config_path)
    known = {s.name: s for s in mcp_cfg.servers}
    unknown = [n for n in names if n not in known]
    if unknown:
        console.print(
            f"[red]✗ unknown MCP server(s): {', '.join(unknown)}. "
            f"Configured: {', '.join(known) or '(none)'} "
            f"[dim]({config_path or _default_mcp_config_hint()})[/][/]")
        sys.exit(2)
    selected = MCPClientConfig(
        servers=[known[n] for n in dict.fromkeys(names)],
        circuit_breaker=mcp_cfg.circuit_breaker,
    )
    mgr = MCPClientManager(selected).start()
    defs, fns = mgr.discover_tools()
    always_defs = [d for d in defs if not mgr.is_write_gated(d.name)]
    gated_defs = [d for d in defs if mgr.is_write_gated(d.name)]
    if read_only and gated_defs:
        console.print(f"[dim]· MCP: {len(gated_defs)} mutating tool(s) "
                      "dropped (--mcp-read-only)[/]")
        fns = {k: v for k, v in fns.items()
               if k not in {d.name for d in gated_defs}}
        gated_defs = []
    up = [s for s in mgr.server_status() if not s["down"]]
    console.print(f"[dim]· MCP: {len(always_defs)} tool(s)"
                  + (f" + {len(gated_defs)} write-gated" if gated_defs else "")
                  + f" from {len(up)} server(s): "
                  + ", ".join(s['name'] for s in up) + "[/]")
    for s in mgr.server_status():
        if s["down"]:
            console.print(f"[yellow]· MCP server {s['name']} DOWN: "
                          f"{s['down_reason']}[/]")
    mcptools.set_surface(mcptools.MCPSurface(
        always_defs=always_defs, gated_defs=gated_defs, fns=fns,
        status_fn=mgr.server_status,
    ))
    return mgr


def _apply_slot_overrides(cfg, chat_model, plan_model, code_model) -> None:
    """Fold --chat/plan/code-model CLI flags into cfg.models + cfg.slots."""
    from luxe.config import ChatSlots, SlotConfig

    overrides = {"chat": chat_model, "plan": plan_model, "code": code_model}
    if not any(overrides.values()):
        return
    if cfg.slots is None:
        cfg.slots = ChatSlots()
    for slot, model_id in overrides.items():
        if not model_id:
            continue
        # Register the model under a synthetic key and point the slot at it.
        key = f"_slot_{slot}"
        cfg.models[key] = model_id
        setattr(cfg.slots, slot, SlotConfig(model_key=key))
