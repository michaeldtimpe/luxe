"""Startup path for `luxe chat` / `luxe code`: resolve → index → lock → run.

Lifted verbatim out of `cli.py` 2026-08-04. `cli.chat_cmd` / `cli.code_cmd`
are thin Click shells over `_run_interactive`; the option list they share lives
here too, so the two postures cannot drift apart.

The handful of names this reaches back into `cli` for (`_resolve_repo`,
`_default_chat_config`, `_default_mcp_config_hint`, `_languages_from_paths`)
are imported INSIDE `_run_interactive`. `cli` imports this module at module
level, so a top-level import back would be circular — and the late binding is
also what keeps `monkeypatch.setattr(cli, ...)` working exactly as before.
"""

from __future__ import annotations

import os
import sys
import time

import click
from rich.console import Console

from luxe import spec_resolver
from luxe import textfmt

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
                          "entry marked default."),
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
                          "their provider keys are configured."),
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
):
    """Shared body of `luxe chat` / `luxe code` (posture via the two kwargs)."""
    from luxe.chat import run_chat_repl
    # Late, and from `cli` on purpose: see the module docstring.
    from luxe.cli import (
        _chat_cfg,
        _default_mcp_config_hint,
        _languages_from_paths,
        _resolve_repo,
        _select_backend,
    )
    from luxe.locks import LockHeld, acquire_repo_lock
    from luxe.tools.fs import set_repo_root

    theme_name = _resolve_theme_name(theme_name)

    repo_path = _resolve_repo(repo)
    cfg = _chat_cfg(config_path)

    # CLI per-slot overrides become an ad-hoc model + slots block so the user
    # can point a slot at any oMLX-loadable model without editing YAML.
    _apply_slot_overrides(cfg, chat_model, plan_model, code_model)

    # `--backend <name>` picks the startup endpoint by re-flagging the config's
    # default entry (chat-only; SlotManager reads default_backend_name()).
    _select_backend(cfg, backend_name)

    # What is this session about? A git root above cwd, a marker-bearing
    # directory, or nothing at all (chat from anywhere). `--repo` given
    # explicitly is honoured as-is; the default "." resolves upward.
    from luxe.chat import project as project_mod

    # ALWAYS resolve upward, flag or not: the `luxe-chat` wrapper passes
    # `--repo "$PWD"` on every invocation, so treating an explicit --repo as
    # "pin exactly here" would silently defeat walk-up for the primary entry
    # point (caught by tests/test_chat_project.py).
    project = project_mod.resolve(repo_path)
    if require_project and not project.is_project:
        # `luxe code` is dead simple on purpose: inside a project it just
        # works; outside one it says so and stops rather than degrading.
        console.print(f"[red]✗ no project at {_tilde(repo_path)} — "
                      "`luxe code` needs a git repo or a marker-bearing "
                      "directory.[/]")
        console.print("[dim]  cd into your project (or pass --repo <path>), "
                      "or use `luxe chat` for a no-project session.[/]")
        sys.exit(2)
    if project.root != repo_path and project.is_project:
        console.print(f"[dim]· {_tilde(repo_path)} is inside "
                      f"{_tilde(project.root)} — using the project root[/]")
    repo_path = project.root
    set_repo_root(repo_path)

    # Cache the `.sdd` contract scan per repo root for this session (chat-only;
    # see spec_resolver). `run_single` rebuilds the contract block every turn
    # and the scan walks the whole root — ~19s per turn with `--repo ~`.
    # `finalize_turn` drops the entry when a turn writes a `.sdd`.
    spec_resolver.enable_scan_cache()

    from luxe import search as search_mod
    from luxe import symbols as symbols_mod

    languages: frozenset[str] = frozenset()
    if project.is_project:
        bm25, sym_idx, scan = _build_chat_indexes(repo_path)
        search_mod.set_index(bm25)
        symbols_mod.set_index(sym_idx)
        # Languages come from the scan we just did — no third walk of the tree.
        languages = _languages_from_paths(scan.paths)
    else:
        # No project here: skip indexing entirely (it cost 210s from `$HOME` for
        # coverage the model can't rely on). Read tools still work against cwd;
        # `/index` or `/project <path>` turns code search on later.
        console.print(
            f"[dim]· no project at {_tilde(repo_path)} — chat mode "
            "(read tools on; `/project <path>` or `/index` to add code "
            "search)[/]")

    # The repo lock serialises luxe runs against ONE codebase; a no-project
    # session has no codebase to protect, and locking `$HOME` would stop you
    # opening a second chat window.
    ctx = None
    if project.is_project:
        try:
            ctx = acquire_repo_lock(repo_path, f"chat-{int(time.time())}")
            ctx.__enter__()
        except LockHeld as e:
            console.print(f"[red]✗ {e}[/]")
            sys.exit(3)

    def _attach_project(target: str | None) -> dict:
        """`/project [path]` and `/index [path]`: re-resolve, re-index, and move
        the repo lock. Returns a summary dict for the command to render; raises
        LockHeld when the new project is busy (the session stays where it is)."""
        nonlocal ctx, repo_path, languages
        new = (project_mod.resolve(target) if target
               else project_mod.resolve(repo_path))
        new_ctx = None
        if new.is_project and new.root != repo_path:
            # Acquire the new lock BEFORE releasing the old one, so a failure
            # leaves the session exactly as it was.
            new_ctx = acquire_repo_lock(new.root, f"chat-{int(time.time())}")
            new_ctx.__enter__()
        if new_ctx is not None:
            if ctx is not None:
                try:
                    ctx.__exit__(None, None, None)
                except Exception:
                    pass
            ctx = new_ctx
        repo_path = new.root
        set_repo_root(repo_path)
        spec_resolver.invalidate_scan_cache()
        search_mod.reset_index()
        symbols_mod.reset_index()
        summary = {"root": new.root, "kind": new.kind, "label": new.label,
                   "files": 0, "symbols": 0, "truncated": "", "used_git": False}
        if new.is_project:
            bm, sy, sc = _build_chat_indexes(new.root)
            search_mod.set_index(bm)
            symbols_mod.set_index(sy)
            languages = _languages_from_paths(sc.paths)
            summary.update(files=len(bm.paths), symbols=len(sy.symbols),
                           truncated=sc.truncated, used_git=sc.used_git)
        else:
            languages = frozenset()
        return summary

    # `--mcp <name>` (chat-only): connect the named servers from the MCP
    # config and publish their tool surface via chat.mcptools; prepare_turn
    # injects it through the extra-tool seam. A server that fails to connect
    # is reported and skipped — an unreachable relay must not stop a chat
    # session from starting (this is the fallback tool).
    mcp_mgr = None
    if mcp_servers:
        from luxe.chat import mcptools
        from luxe.mcp.client import (
            MCPClientConfig,
            MCPClientManager,
            load_mcp_config,
        )
        mcp_cfg = load_mcp_config(mcp_config_path)
        known = {s.name: s for s in mcp_cfg.servers}
        unknown = [n for n in mcp_servers if n not in known]
        if unknown:
            console.print(
                f"[red]✗ unknown MCP server(s): {', '.join(unknown)}. "
                f"Configured: {', '.join(known) or '(none)'} "
                f"[dim]({mcp_config_path or _default_mcp_config_hint()})[/][/]")
            sys.exit(2)
        selected = MCPClientConfig(
            servers=[known[n] for n in dict.fromkeys(mcp_servers)],
            circuit_breaker=mcp_cfg.circuit_breaker,
        )
        mcp_mgr = MCPClientManager(selected).start()
        defs, fns = mcp_mgr.discover_tools()
        always_defs = [d for d in defs if not mcp_mgr.is_write_gated(d.name)]
        gated_defs = [d for d in defs if mcp_mgr.is_write_gated(d.name)]
        if mcp_read_only and gated_defs:
            console.print(f"[dim]· MCP: {len(gated_defs)} mutating tool(s) "
                          "dropped (--mcp-read-only)[/]")
            fns = {k: v for k, v in fns.items()
                   if k not in {d.name for d in gated_defs}}
            gated_defs = []
        up = [s for s in mcp_mgr.server_status() if not s["down"]]
        console.print(f"[dim]· MCP: {len(always_defs)} tool(s)"
                      + (f" + {len(gated_defs)} write-gated" if gated_defs else "")
                      + f" from {len(up)} server(s): "
                      + ", ".join(s['name'] for s in up) + "[/]")
        for s in mcp_mgr.server_status():
            if s["down"]:
                console.print(f"[yellow]· MCP server {s['name']} DOWN: "
                              f"{s['down_reason']}[/]")
        mcptools.set_surface(mcptools.MCPSurface(
            always_defs=always_defs, gated_defs=gated_defs, fns=fns,
            status_fn=mcp_mgr.server_status,
        ))

    # Front-end selection: the full-screen Textual TUI when stdout is a real
    # terminal AND textual is installed; otherwise the line REPL (CI / pipes /
    # textual-absent). --resume rides either front-end (the TUI replays the
    # transcript on mount). chat.sdd.
    run_app = None
    if sys.stdout.isatty():
        try:
            from luxe.chat.tui import run_chat_app as run_app
        except Exception:
            run_app = None
            console.print("[dim]· textual not installed — using the line REPL. "
                          "Run `uv sync --extra chat` (repo root) to restore "
                          "the full-screen UI.[/]")

    try:
        if run_app is not None:
            run_app(
                cfg, repo_path, languages,
                keep_loaded=keep_loaded,
                resume_session_id=resume_session_id,
                dev_mode=dev_mode,
                start_web=start_web,
                start_write=start_write,
                startup_verbose=startup_verbose,
                startup_show_reasoning=startup_show_reasoning,
                startup_no_terse=startup_no_terse,
                startup_debug=startup_debug,
                startup_compact=startup_compact,
                theme_name=theme_name,
                startup_ctx_tier=ctx_tier,
                on_project=_attach_project,
                project_kind=project.kind,
            )
        else:
            run_chat_repl(
                cfg, repo_path, languages,
                console=console,
                keep_loaded=keep_loaded,
                resume_session_id=resume_session_id,
                dev_mode=dev_mode,
                start_web=start_web,
                start_write=start_write,
                startup_verbose=startup_verbose,
                startup_show_reasoning=startup_show_reasoning,
                startup_no_terse=startup_no_terse,
                startup_debug=startup_debug,
                startup_compact=startup_compact,
                theme_name=theme_name,
                startup_ctx_tier=ctx_tier,
                on_project=_attach_project,
                project_kind=project.kind,
            )
    finally:
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
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass


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
