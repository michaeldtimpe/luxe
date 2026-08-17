"""CLI entry point for luxe — mono-only execution."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import click
from rich.console import Console

from luxe.paths import luxe_home
from luxe import gitcmd
from luxe import textfmt
from luxe import gitclone
from luxe.agents.tasktype import infer_task_type
# Language detection moved to repo_index (the de-facto home for
# extension→language tables). Re-exported: tests and
# scripts/chunk_conclude_ab.py import them from `luxe.cli`.
from luxe.repo_index import (  # noqa: F401  (re-exports)
    _LANG_BY_EXT,
    _detect_languages_for_repo,
    _languages_from_paths,
)
from luxe.config import load_config

console = Console()


# Moved to their tier homes 2026-08-05 (deferred-list #6); re-exported here
# for the tests and any external caller that knows the old private names.
_resolve_repo = gitclone.resolve_repo
_infer_task_type = infer_task_type


class AliasedGroup(click.Group):
    """A click Group that resolves alias names to canonical command names.

    Centralizes alias logic (vs. registering duplicate command objects):
    overrides both `get_command` (lookup-time canonicalization) and
    `resolve_command` (so `--help`/usage shows the canonical name)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aliases: dict[str, str] = {}

    def get_command(self, ctx, cmd_name):
        return super().get_command(ctx, self._aliases.get(cmd_name, cmd_name))

    def resolve_command(self, ctx, args):
        if args and args[0] in self._aliases:
            args = [self._aliases[args[0]], *args[1:]]
        return super().resolve_command(ctx, args)


def apply_aliases(group: AliasedGroup, alias_map: dict[str, str]) -> AliasedGroup:
    """Register `alias -> canonical` command-name mappings on an AliasedGroup."""
    group._aliases.update(alias_map)
    return group


@click.group(cls=AliasedGroup)
def main():
    """luxe — MLX-only repo maintainer."""
    pass


@main.command()
@click.argument("repo")
@click.argument("goal")
@click.option("--task", "task_type", default=None,
              type=click.Choice(["review", "implement", "bugfix", "document", "summarize", "manage"]),
              help="Task type (default: auto-detected from goal)")
@click.option("--config", "config_path", default=None,
              help="Path to config YAML (default: configs/single_64gb.yaml)")
@click.option("--allow-dirty", is_flag=True,
              help="Permit running with an uncommitted working tree (foot-gun; "
                   "PR diff WILL include your changes)")
@click.option("--yes", "skip_confirm", is_flag=True,
              help="Skip TTY confirmations (e.g. for --allow-dirty in scripts)")
@click.option("--watch-ci", is_flag=True,
              help="After PR is opened, poll `gh pr checks` and convert "
                   "draft→ready (or vice versa) based on CI result")
@click.option("--output", "output_dir", default="./runs", help="Directory for run artefacts")
@click.option("--save-report", is_flag=True, help="Save final report as markdown to --output")
@click.option("--keep-loaded", is_flag=True, default=False,
              help="Skip the post-run model unload. By default luxe maintain "
                   "unloads every model it touched once the run completes, "
                   "freeing oMLX RAM. Pass --keep-loaded to keep them warm "
                   "for a follow-up run.")
@click.option("--spec-yaml", "spec_yaml_path", default=None,
              help="Path to a YAML file containing a SpecDD spec (Lever 1, "
                   "v1.4-prep). When provided AND LUXE_REPROMPT_ON_DOC=1, "
                   "the reprompt gate uses per-requirement spec validation "
                   "instead of the diff-size heuristic. Without this flag, "
                   "the v1.3 reprompt behavior is preserved.")
def maintain(
    repo: str, goal: str, task_type: str | None,
    config_path: str | None,
    allow_dirty: bool, skip_confirm: bool, watch_ci: bool,
    output_dir: str, save_report: bool, keep_loaded: bool,
    spec_yaml_path: str | None,
):
    """Run a luxe maintain pipeline against a repository.

    REPO: Local path or git URL to clone.
    GOAL: What to accomplish (e.g., "fix the off-by-one in pagination").
    """
    maintain_pipeline(
        repo, goal, task_type, config_path, allow_dirty, skip_confirm,
        watch_ci, output_dir, save_report, keep_loaded, spec_yaml_path,
    )


# `luxe chat` / `luxe code` — thin Click shells. The shared option list, the
# posture wiring, and the whole startup path live in chat/launch.py. The
# non-shell names are re-exported so `luxe.cli._X` keeps resolving for the
# tests and scripts that import them there.
from luxe.maintain import (  # noqa: E402,F401  (shell + re-exports)
    _default_config,
    _diff_against_base,
    _should_reprompt_for_under_engagement,
    _WRITE_TASKS,
    maintain_pipeline,
)
from luxe.chat.launch import (  # noqa: E402,F401  (re-exports)
    _INDEX_MAX_FILES,
    _INDEX_MAX_MB,
    _apply_slot_overrides,
    _build_chat_indexes,
    _resolve_theme_name,
    _run_interactive,
    _shared_chat_options,
    _tilde,
)


@main.command(name="chat")
@_shared_chat_options
def chat_cmd(**kwargs):
    """Interactive terminal agent (Claude-CLI-style). Starts anywhere; default:
    the host manifest's main model in every slot, read-only tools (toggle with
    /write). Use `luxe code` for a project-first, write-on session."""
    _run_interactive(require_project=False, start_write=False, **kwargs)


@main.command(name="code")
@_shared_chat_options
def code_cmd(**kwargs):
    """Project coding session: same engine as `luxe chat`, different posture —
    REQUIRES a project (git root or marker directory; errors out otherwise)
    and starts with write tools ON. Bash stays gated (/bash to enable)."""
    _run_interactive(require_project=True, start_write=True, **kwargs)


def _chat_cfg(config_path: str | None = None):
    """The chat config: `--config <path>` when given, else configs/chat.yaml.

    One spelling for what was nine copies in this file plus one in
    chat/launch.py. Resolves `_default_chat_config` through THIS module's
    globals, which is what the tests that monkeypatch it rely on.
    """
    return load_config(config_path or _default_chat_config())


def _select_backend(cfg, backend_name: str | None, *,
                    make_default: bool = True) -> None:
    """Validate `--backend <name>` against `backends:` and re-flag it default.

    Exits 2, naming every configured entry, on a miss — a typo must not fall
    through to the default endpoint and look like it worked. `make_default`
    is False for `luxe smoke`, which validates the name but chooses its
    endpoint itself.
    """
    if not backend_name:
        return
    entries = cfg.backend_entries()
    if backend_name not in entries:
        console.print(f"[red]✗ Unknown backend {backend_name!r}. "
                      f"Configured: {', '.join(entries)}.[/]")
        sys.exit(2)
    if make_default:
        cfg.backends = {k: v.model_copy(update={"default": k == backend_name})
                        for k, v in entries.items()}


def _unload_unless(keep_loaded: bool) -> None:
    """Post-command teardown: free the local oMLX's RAM unless --keep-loaded.

    Best-effort by design — a teardown failure must never mask (or fail) the
    command whose `finally` this runs in. `maintain` keeps its own copy: it
    also REPORTS what it unloaded.
    """
    if keep_loaded:
        return
    from luxe.backend import Backend
    try:
        Backend(model="(unload-probe)").unload_all_loaded()
    except Exception:
        pass


def _default_chat_config() -> str:
    """The chat config to use when no `--config` was passed.

    `$LUXE_CONFIG` wins over the in-tree default so a host whose engine is not
    the fleet's can point EVERY command at its own config, not just the two
    the dotfiles wrappers cover. That gap bit on neo (2026-08-13): `luxe-chat`
    and `luxe-code` passed `--config ~/dotfiles/luxe/neo.yaml`, but bare
    `luxe ready` / `luxe smoke` / `luxe pull` still read the fleet config and
    judged an oMLX endpoint that box does not run — and `luxe ready` is
    precisely the command reached for in a panic.

    Deliberately an env var and not a path lookup: the per-host configs live
    OUT of this repo (`~/dotfiles/luxe/<host>.yaml` — see the wrapper's own
    note on the 2026-08-02 skip-worktree drift), and hardcoding a dotfiles
    path here would drag a private layout into the public tree. Unset ⇒ the
    previous behaviour exactly. Chat-config only: the benchmark/maintain
    config surface (`--variants`, `single_64gb.yaml`) never routes through
    here.
    """
    override = os.environ.get("LUXE_CONFIG", "").strip()
    if override:
        return str(Path(override).expanduser())
    return str(Path(__file__).parent.parent.parent / "configs" / "chat.yaml")


def _default_mcp_config_hint() -> str:
    from luxe.mcp.client import default_mcp_config_path
    return str(default_mcp_config_path())


@main.group(name="compare")
def compare_group():
    """Run or review side-by-side single-task comparisons."""


@compare_group.command(name="run")
@click.argument("task")
@click.option("--repo", default=".", help="Repo to work in (default: cwd)")
@click.option("--config", "config_path", default=None, help="Config YAML (default: chat.yaml)")
@click.option("--mode", type=click.Choice(["1", "2", "3"]), default="1",
              help="1=luxe-vs-bare 2=two-prompts 3=vs-another-model")
@click.option("--model-b", default=None, help="Second model id (mode 3)")
@click.option("--prompt-a", default="baseline", help="Prompt variant A (mode 2)")
@click.option("--prompt-b", default="cot", help="Prompt variant B (mode 2)")
@click.option("--blind", is_flag=True, help="Hide which side is which before voting")
@click.option("--keep-loaded", is_flag=True, default=False)
def compare_run_cmd(task, repo, config_path, mode, model_b, prompt_a, prompt_b, blind, keep_loaded):
    """Run TASK through two configurations and present them side by side."""
    from luxe.compare import build_sides, run_compare
    from luxe.compare import present, store
    from luxe.tools.fs import set_repo_root

    repo_path = _resolve_repo(repo)
    cfg = _chat_cfg(config_path)
    set_repo_root(repo_path)

    from luxe import search as search_mod
    from luxe import symbols as symbols_mod
    console.print("[dim]· Building BM25 + symbol indices…[/]")
    search_mod.set_index(search_mod.build_bm25_index(repo_path))
    symbols_mod.set_index(symbols_mod.build_symbol_index(repo_path))
    languages = _detect_languages_for_repo(repo_path)

    champion = cfg.model_for_slot("chat")
    side_a, side_b = build_sides(
        int(mode), model_id=champion, model_b=model_b,
        prompt_a=prompt_a, prompt_b=prompt_b,
    )
    try:
        console.print("[dim]· running side A, then side B (sequential)…[/]")
        result = run_compare(
            side_a, side_b,
            task=task, task_type=_infer_task_type(task), languages=languages,
            omlx_base_url=cfg.omlx_base_url, blind=blind,
            on_status=lambda m: console.print(f"[dim]· {m}[/]"),
        )
        store.save(result)
        present.render_side_by_side(console, result)
        present.prompt_vote(console, result)
    finally:
        search_mod.reset_index()
        symbols_mod.reset_index()
        _unload_unless(keep_loaded)


@compare_group.command(name="review")
@click.argument("compare_id", required=False, default="")
def compare_review_cmd(compare_id):
    """Replay a stored comparison and tally its votes (no arg: list them)."""
    from luxe.compare import store
    store.review(compare_id, console=console)


def _run_gitkit_cmd(kind: str, repo: str, config_path: str | None,
                    keep_loaded: bool, save: bool, verbose: bool = False,
                    deep: bool | None = None, max_chunks: int | None = None,
                    rebuild_map: bool = False, mirror: bool = True,
                    base: str | None = None, pr: int | None = None,
                    min_severity: str | None = None,
                    no_incremental: bool = False) -> None:
    """Shared body for the gitaudit/gitchange CLI commands. The runner owns target
    resolution (incl. cloning a URL when the path is not a git repo), index
    building, and repo_root; here we only clone an explicit URL arg up front,
    load the config, and unload models afterward.

    `deep` (None=auto by footprint, True/False force), `max_chunks`, and
    `rebuild_map` pass straight through to the runner's deep-mode dispatch."""
    from luxe.gitkit import run_git_report

    # An explicit URL arg clones immediately (no prompt). A local path is passed
    # through; the runner prompts to clone if it isn't a git working tree.
    if repo.startswith(("http://", "https://", "git@", "ssh://")):
        repo_path = _resolve_repo(repo, full_history=(kind == "gitaudit"))
    else:
        repo_path = str(Path(repo).expanduser().resolve())
    cfg = _chat_cfg(config_path)

    try:
        run_git_report(kind, cfg=cfg, repo_path=repo_path,
                       console=console, save=save, verbose=verbose,
                       deep=deep, max_chunks=max_chunks, rebuild_map=rebuild_map,
                       mirror=mirror, base=base, pr=pr,
                       min_severity=min_severity, no_incremental=no_incremental)
    finally:
        _unload_unless(keep_loaded)


def _run_gitapply_cmd(repo: str, config_path: str | None, keep_loaded: bool,
                      *, deep: bool | None = None, rebuild_map: bool = False) -> None:
    """Body for `gitchange --apply` / `gitapply`: execute a saved plan against a local
    repo. Apply NEVER clones — it only runs on a real checkout the user controls."""
    from luxe.gitkit import apply as apply_mod

    if repo.startswith(("http://", "https://", "git@", "ssh://")):
        console.print("[red]gitapply does not clone — point it at a local repo path.[/]")
        raise SystemExit(2)
    repo_path = str(Path(repo).expanduser().resolve())
    cfg = _chat_cfg(config_path)
    try:
        rc = apply_mod.run_apply(repo_path=repo_path, cfg=cfg, console=console,
                                 deep=deep, rebuild_map=rebuild_map)
    finally:
        _unload_unless(keep_loaded)
    raise SystemExit(rc)


def _gitkit_options(f):
    """Shared options for the two gitkit commands (incl. deep-mode flags)."""
    f = click.argument("repo", required=False, default=".")(f)
    f = click.option("--config", "config_path", default=None,
                     help="Config YAML (default: chat.yaml)")(f)
    f = click.option("--keep-loaded", is_flag=True, default=False)(f)
    f = click.option("--no-save", is_flag=True, default=False,
                     help="Print only; don't save the report")(f)
    f = click.option("--verbose", "-v", is_flag=True, default=False,
                     help="Print the full report on screen (default: preview + saved path)")(f)
    f = click.option("--deep/--no-deep", "deep", default=None,
                     help="Force staged deep mode on/off (default: auto by repo size)")(f)
    f = click.option("--max-chunks", "max_chunks", type=int, default=None,
                     help="Deep mode: cap chunks analyzed (default: unlimited)")(f)
    f = click.option("--rebuild-map", is_flag=True, default=False,
                     help="Deep mode: ignore the cached per-repo map and re-survey")(f)
    f = click.option("--no-incremental", is_flag=True, default=False,
                     help="Deep mode: don't reuse cached per-chunk notes "
                          "(re-analyze every chunk even when unchanged)")(f)
    f = click.option("--no-mirror", is_flag=True, default=False,
                     help="Don't write the committable <repo>/.luxe/gitkit/ mirror")(f)
    return f


@main.command(name="gitaudit")
@_gitkit_options
@click.option("--base", "base", default=None, metavar="REF",
              help="Diff audit: analyze ONLY the change between REF (merge-base) "
                   "and HEAD. Mutually exclusive with --pr.")
@click.option("--pr", "pr", type=int, default=None, metavar="N",
              help="Diff audit of GitHub PR #N's changes (base resolved via gh). "
                   "Mutually exclusive with --base.")
@click.option("--min-severity", "min_severity",
              type=click.Choice(["low", "medium", "high", "critical"]),
              default=None,
              help="Display-side filter: hide findings below this severity "
                   "(the saved report is always complete).")
def gitaudit_cmd(repo, config_path, keep_loaded, no_save, verbose,
                 deep, max_chunks, rebuild_map, no_incremental, no_mirror,
                 base, pr, min_severity):
    """Audit a repo (read-only): orientation + bugs/security + structural advice."""
    if base is not None and pr is not None:
        console.print("[red]--base and --pr are mutually exclusive.[/]")
        raise SystemExit(2)
    _run_gitkit_cmd("gitaudit", repo, config_path, keep_loaded, not no_save,
                    verbose, deep=deep, max_chunks=max_chunks,
                    rebuild_map=rebuild_map, mirror=not no_mirror,
                    base=base, pr=pr, min_severity=min_severity,
                    no_incremental=no_incremental)


@main.command(name="gitchange")
@_gitkit_options
@click.option("--apply", "do_apply", is_flag=True, default=False,
              help="Execute the plan: branch, apply each step in WRITE mode, "
                   "diff+test+confirm. Interactive-only; never touches main.")
def gitchange_cmd(repo, config_path, keep_loaded, no_save, verbose,
                  deep, max_chunks, rebuild_map, no_incremental, no_mirror,
                  do_apply):
    """Produce an apply-ready structural change plan (read-only); --apply executes it."""
    if do_apply:
        _run_gitapply_cmd(repo, config_path, keep_loaded, deep=deep,
                          rebuild_map=rebuild_map)
    else:
        _run_gitkit_cmd("gitchange", repo, config_path, keep_loaded, not no_save,
                        verbose, deep=deep, max_chunks=max_chunks,
                        rebuild_map=rebuild_map, mirror=not no_mirror,
                        no_incremental=no_incremental)


@main.command(name="gitapply")
@click.argument("repo", required=False, default=".")
@click.option("--config", "config_path", default=None,
              help="Config YAML (default: chat.yaml)")
@click.option("--keep-loaded", is_flag=True, default=False)
@click.option("--deep/--no-deep", "deep", default=None,
              help="If no saved plan exists, force deep/single when generating one")
@click.option("--rebuild-map", is_flag=True, default=False)
def gitapply_cmd(repo, config_path, keep_loaded, deep, rebuild_map):
    """Execute a saved gitchange plan: branch, apply each step, diff+test+confirm."""
    _run_gitapply_cmd(repo, config_path, keep_loaded, deep=deep,
                      rebuild_map=rebuild_map)


# Back-compat aliases. The old four commands are now two: gitsummary/gitreview/
# gitrefactor → gitaudit (combined read-only analysis); gitplan → gitchange.
apply_aliases(main, {
    "git-audit": "gitaudit", "gaudit": "gitaudit",
    "gitsummary": "gitaudit", "git-summary": "gitaudit", "gsum": "gitaudit",
    "gitreview": "gitaudit", "git-review": "gitaudit", "grev": "gitaudit",
    "gitrefactor": "gitaudit", "git-refactor": "gitaudit", "gref": "gitaudit",
    "git-change": "gitchange", "gchange": "gitchange",
    "gitplan": "gitchange", "git-plan": "gitchange", "gplan": "gitchange",
    # `luxe doctor` is the name people reach for under pressure; `ready` is
    # the canonical one (it answers "can I work right now?").
    "doctor": "ready",
})


@main.command(name="unload")
@click.option("--except", "except_for", multiple=True,
              help="Model ID(s) to keep resident (repeatable). Default: unload all.")
def unload_models(except_for: tuple[str, ...]):
    """Unload all currently-loaded models from oMLX to free RAM."""
    from luxe.backend import Backend
    b = Backend(model="(unload-cli)")
    if not b.health():
        console.print("[red]oMLX unreachable — is `brew services start omlx` running?[/]")
        sys.exit(2)
    loaded = b.loaded_models()
    if not loaded:
        console.print("[dim]No models currently loaded — nothing to unload.[/]")
        return
    keep = set(except_for or [])
    console.print(f"Loaded models: {len(loaded)}")
    for m in loaded:
        marker = "[dim](kept)[/]" if m in keep else ""
        console.print(f"  · {m} {marker}")
    results = b.unload_all_loaded(except_for=list(keep))
    n_ok = sum(1 for v in results.values() if v)
    console.print(f"\n[bold]Unloaded {n_ok}/{len(results)} model(s)[/]")
    if n_ok < len(results):
        for mid, ok in results.items():
            if not ok:
                console.print(f"  [yellow]✗ {mid} — unload failed[/]")


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
            if not from_path and not force_hf:
                console.print("[dim]· scanning mounted volumes…[/]")
            sources = ms.resolve_pull_sources(
                ref, admin=admin, from_path=from_path,
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
            if name in ms.local_model_names(dest_dir) and not force:
                console.print(f"[yellow]· {name} is already in {dest_dir} "
                              "— pass --force to replace it.[/]")
                sys.exit(0)
            if not assume_yes and not click.confirm("Pull it?", default=True):
                console.print("[dim]· cancelled[/]")
                return

            if chosen.kind == "mount":
                _pull_from_mount(chosen, dest_dir, force)
            else:
                _pull_from_hf(admin, chosen)
        except ms.ModelStoreError as e:
            console.print(f"[red]✗ {e}[/]")
            sys.exit(4)
        except KeyboardInterrupt:
            console.print("\n[yellow]· interrupted (partial copy removed)[/]")
            sys.exit(130)


@main.command(name="update")
@click.option("--no-sync", is_flag=True, default=False,
              help="Skip `uv sync` after pulling (code only, no dep changes).")
def update_cmd(no_sync: bool):
    """Update THIS host's luxe checkout: fetch → rebase onto origin/main →
    `uv sync` with the canonical extras (chat+dev+analyzers+web). Says what's
    incoming before touching anything; no-op (and says so) when already
    current. Targets the luxe source repo regardless of where you run it."""
    import subprocess as sp

    from luxe import buildinfo

    root = buildinfo._repo_root()
    old, dirty = buildinfo.version_parts()
    console.print(f"[bold]luxe[/] [dim]{root}[/] · version {old}"
                  + (" [yellow](dirty — rebase will autostash)[/]" if dirty
                     else ""))

    with console.status("[dim]fetching origin…[/]"):
        fetched = buildinfo.fetch_origin(timeout_s=20)
    if not fetched:
        console.print("[red]✗ couldn't fetch origin[/] [dim](offline, or no "
                      "remote configured) — try again with network[/]")
        sys.exit(1)

    behind = buildinfo.behind_origin()
    if behind == 0:
        console.print("[green]✓ already current with origin/main[/]")
        return

    def _git(*args, timeout=120):
        return gitcmd.run(root, *args, timeout=timeout)

    log = _git("log", "--oneline", "HEAD..origin/main")
    console.print(f"[bold]{behind} commit(s) incoming:[/]")
    for line in log.stdout.strip().splitlines()[:15]:
        console.print(f"  [dim]{line}[/]")

    pulled = _git("pull", "--rebase")
    if pulled.returncode != 0:
        console.print(f"[red]✗ git pull --rebase failed:[/]\n"
                      f"[dim]{(pulled.stderr or pulled.stdout).strip()}[/]")
        sys.exit(1)

    if not no_sync:
        # chat = TUI; dev = pytest (the --code drill runs it via the venv
        # python, so it's a RUNTIME need on every host); analyzers = the
        # lint/typecheck/security tools shell-outs; web = playwright, after
        # the 2026-08-05 fleet deployment of web_page/render — every sync
        # WITHOUT it pruned the package and silently withheld the tools
        # (same failure shape as the 2026-07-30 dev prune that broke the
        # drill). The Chromium download stays per-host and outside the venv.
        extras = ["--extra", "chat", "--extra", "dev", "--extra", "analyzers",
                  "--extra", "web"]
        with console.status("[dim]uv sync (chat+dev+analyzers+web)…[/]"):
            try:
                synced = sp.run(["uv", "sync", *extras],
                                cwd=str(root), capture_output=True, text=True,
                                timeout=600)
            except FileNotFoundError:
                synced = None
        if synced is None:
            console.print("[yellow]⚠ uv not on PATH — run "
                          "`uv sync --extra chat --extra dev --extra "
                          "analyzers --extra web` in the repo yourself[/]")
        elif synced.returncode != 0:
            console.print(f"[yellow]⚠ uv sync failed:[/]\n"
                          f"[dim]{synced.stderr.strip()[-500:]}[/]")

    new, _ = buildinfo.version_parts()
    console.print(f"[bold][green]✓[/] {old} → {new}[/] [dim]— restart any "
                  "open chat/code sessions to pick it up[/]")


@main.command(name="smoke")
@click.option("--config", "config_path", default=None,
              help="Config YAML (default: configs/chat.yaml)")
@click.option("--backend", "backend_name", default=None,
              help="Run against this configured backends: entry (e.g. m5). "
                   "Drill models resolve from the TARGET host's manifest.")
@click.option("--base-url", default="",
              help="oMLX endpoint to smoke (default: the config's default backend)")
@click.option("--code", "code_drill", is_flag=True, default=False,
              help="Run the CODING drill instead: plant a bug + failing test "
                   "in a scratch repo, let the model fix it, verify with "
                   "pytest + git diff.")
@click.option("--chat", "chat_drill", is_flag=True, default=False,
              help="Run the CHAT drill instead: a read-only turn that must "
                   "read a file to answer.")
@click.option("--skip-fallback", is_flag=True, default=False,
              help="Skip the fallback-model leg (it pays a full weight swap).")
@click.option("--skip-tools", is_flag=True, default=False,
              help="Skip the tool-call turn.")
@click.option("--keep-loaded", is_flag=True, default=False,
              help="Leave the last smoked model resident.")
@click.option("--expect-model", default="",
              help="Identity preflight: fail (exit 2) unless the target "
                   "endpoint serves a model whose id contains this "
                   "substring. Run before n-rep acceptance nights — a "
                   "health check is not an identity check.")
@click.option("--model", "model_override", default=None,
              help="Drill this cached model instead of the manifest main "
                   "(--chat/--code only — e.g. the m5 capacity model, which "
                   "is a keep:, never a main). The default kit drill stays "
                   "manifest-driven.")
def smoke_cmd(config_path: str | None, backend_name: str | None,
              base_url: str, code_drill: bool, chat_drill: bool,
              skip_fallback: bool, skip_tools: bool, keep_loaded: bool,
              expect_model: str, model_override: str | None):
    """Aliveness drills for this host's fallback kit (minutes, not a bench).

    Default: manifest → weights → endpoint → catalog → one real turn + tool
    call on main → one turn on the fallback. `--code` / `--chat` run the
    agentic drills instead (combinable): real run_single turns against a
    planted scratch repo — the full coding pipeline, deterministically
    verified. `--backend m5` drills a remote host's models from here.
    Exit 0 = ready; exit 1 = something needs fixing (each line says what).
    """
    from luxe.chat.smoke import run_chat_drill, run_code_drill, run_smoke

    cfg = _chat_cfg(config_path)
    # `luxe smoke` picks its own endpoint (--base-url / the manifest), so the
    # name is validated but never promoted to default.
    _select_backend(cfg, backend_name, make_default=False)
    t0 = time.time()
    glyphs = {"pass": "[green]✓[/]", "warn": "[yellow]⚠[/]", "fail": "[red]✗[/]"}

    if expect_model:
        from luxe.chat.smoke import check_expected_model
        ok, detail = check_expected_model(cfg, expect_model,
                                          base_url=base_url or None,
                                          backend_name=backend_name)
        console.print(f"  {glyphs['pass' if ok else 'fail']} identity — {detail}")
        if not ok:
            sys.exit(2)

    reports = []
    if code_drill or chat_drill:
        if chat_drill:
            console.print("[bold]chat drill[/]")
            reports.append(run_chat_drill(cfg, backend_name=backend_name,
                                          base_url=base_url or None,
                                          model=model_override))
            for step in reports[-1].steps:
                console.print(f"  {glyphs[step.state]} {step.name} — {step.detail}")
        if code_drill:
            console.print("[bold]code drill[/]")
            reports.append(run_code_drill(cfg, backend_name=backend_name,
                                          base_url=base_url or None,
                                          model=model_override))
            for step in reports[-1].steps:
                console.print(f"  {glyphs[step.state]} {step.name} — {step.detail}")
    else:
        if model_override:
            console.print("[yellow]⚠ --model applies to --chat/--code drills "
                          "only; the kit drill is manifest-driven.[/]")
        reports.append(run_smoke(cfg, base_url=base_url or None,
                                 skip_fallback=skip_fallback,
                                 skip_tools=skip_tools))
        for step in reports[-1].steps:
            console.print(f"  {glyphs[step.state]} {step.name} — {step.detail}")

    failed = any(r.failed for r in reports)
    if not keep_loaded and not backend_name:
        # Only unload the endpoint we own; a remote host's residency is its
        # own business (never unload a server another session may be using).
        try:
            from luxe.backend import Backend
            from luxe.secrets import resolve_api_key
            entry = cfg.backend_entry(cfg.default_backend_name())
            Backend(base_url=base_url or entry.base_url, model="",
                    api_key=resolve_api_key(entry.api_key_env)
                    ).unload_all_loaded()
        except Exception:
            pass
    verdict = ("[red]NOT READY[/]" if failed else "[green]READY[/]")
    console.print(f"[bold]{verdict}[/] [dim]({time.time() - t0:.0f}s)[/]")
    sys.exit(1 if failed else 0)


def build_ready_doctor(cfg, repo_path: str):
    """Build the host-level `Doctor` for `luxe ready` — no REPL, no model.

    Reuses `/doctor`'s checks verbatim against a stand-in session so the two
    surfaces can never disagree; `hostwide_view` then restates the lines that
    only mean something inside a session. Split out of `ready_cmd` so tests can
    assert render parity with `/doctor` on the same inputs.
    """
    from luxe.chat import inspection
    from luxe import project as project_mod
    from luxe.chat.origin import host_for_endpoint
    from luxe.chat.session import ChatSession
    from luxe.chat.slots import SlotManager

    project = project_mod.resolve(repo_path)
    session = ChatSession(repo_path=project.root, project_kind=project.kind)
    # `luxe ready` is a DRILL, not a session: models and manifest resolve from
    # the host the active endpoint POINTS AT (chat.sdd drill rule, same as
    # smoke) — `--backend m5` judges m5's pair against m5's catalog. For the
    # local default this is short_hostname(), i.e. exactly the session rule.
    entry = cfg.backend_entry(cfg.default_backend_name())
    slots = SlotManager(cfg, manifest_host=host_for_endpoint(entry.base_url))
    doc = inspection.run_doctor(session, slots, project.root)
    return inspection.hostwide_view(doc)


@main.command(name="ready")
@click.option("--config", "config_path", default=None,
              help="Config YAML (default: configs/chat.yaml)")
@click.option("--backend", "backend_name", default=None,
              help="Check this configured backends: entry (e.g. m5) instead "
                   "of the default one.")
@click.option("--repo", default=".",
              help="Directory to judge the project checks against (default: cwd)")
def ready_cmd(config_path: str | None, backend_name: str | None, repo: str):
    """Can I work right now? Point-in-time host preflight — seconds, no model.

    The same table `/doctor` prints inside a session: endpoint, oMLX build,
    key, model, weights, manifest, disk, update, git. Exit 0 = ready (warnings
    included), exit 1 = something is broken. Every ✗/! line carries a
    runnable fix. Offline-safe: the ≤4s `update` fetch is the only network
    call and degrades quietly.
    """
    from luxe.chat import inspection

    t0 = time.time()
    cfg = _chat_cfg(config_path)
    _select_backend(cfg, backend_name)

    doc = build_ready_doctor(cfg, str(Path(repo).expanduser()))
    worst = inspection.render_doctor(doc, console, title="luxe ready")

    if worst == inspection.FAIL:
        console.print(f"[bold][red]NOT READY[/][/] "
                      f"[dim]({time.time() - t0:.0f}s)[/] — fix the ✗ lines "
                      "above")
        console.print("[dim]offline emergency card: `luxe outage`[/]")
        sys.exit(1)
    label = ("[green]READY[/]" if worst == inspection.OK
             else "[yellow]READY (warnings)[/]")
    console.print(f"[bold]{label}[/] [dim]({time.time() - t0:.0f}s)[/]")
    console.print("[dim]full generation drill: `luxe smoke` · agentic drill: "
                  "`luxe smoke --chat --code`[/]")
    sys.exit(0)


@main.command(name="init")
@click.argument("path", default=".")
@click.option("--config", "config_path", default=None,
              help="Config YAML (default: configs/chat.yaml)")
@click.option("--dry-run", is_flag=True, default=False,
              help="Print the brief instead of writing it.")
@click.option("--keep-loaded", is_flag=True, default=False,
              help="Leave the model resident afterwards.")
def init_cmd(path: str, config_path: str | None, dry_run: bool,
             keep_loaded: bool):
    """Draft this repo's orientation brief into `.luxe/memory.md`.

    One read-only pass over the repo (health + map + framing files) produces a
    ≤50-line project brief — what this is, stack, layout, how to run and test
    it, invariants and gotchas — written into a fenced `luxe:brief` block.
    Everything you write in that file yourself is preserved; re-running
    replaces only the block. From then on, every `luxe chat` / `luxe code`
    session in this repo starts already oriented.
    """
    from luxe.gitkit import brief as brief_mod

    cfg = _chat_cfg(config_path)
    result = brief_mod.run_init(path, cfg, console=console, dry_run=dry_run)
    if not result.ok:
        console.print(f"[red]✗ {result.error}[/]")
        sys.exit(1)

    if not keep_loaded:
        try:
            from luxe.backend import Backend
            Backend(base_url=cfg.omlx_base_url, model="").unload_all_loaded()
        except Exception:
            pass

    if dry_run:
        from rich.markdown import Markdown
        console.print(Markdown(result.text))
        console.print("[dim]· --dry-run: nothing written[/]")
        sys.exit(0)
    console.print(f"[green]✓[/] brief → {result.written} "
                  f"[dim]({len(result.text)} chars"
                  f"{', truncated' if result.truncated else ''})[/]")
    console.print("[dim]  injected as <project_memory> in every session here; "
                  "edit the file freely — re-running replaces only the fenced "
                  "block[/]")
    sys.exit(0)


@main.command(name="outage")
@click.option("--plain", is_flag=True, default=False,
              help="Print the raw markdown (no Rich rendering).")
def outage_cmd(plain: bool):
    """Print the offline emergency card (OUTAGE.md).

    Zero network, zero model, no config: it works with oMLX stopped and the
    link down. `luxe ready` points here when it says NOT READY.
    """
    from luxe.outage import load_card

    text = load_card()
    if plain or not console.is_terminal:
        click.echo(text)
    else:
        from rich.markdown import Markdown
        console.print(Markdown(text))
    sys.exit(0)


@main.command(name="net")
@click.option("--host", default=None,
              help="Hostname for the public ladder (default: a public anchor)")
@click.option("--config", "config_path", default=None,
              help="Pipeline config (default: the chat config)")
@click.option("--watch", "watch_s", default=0, type=int,
              help="Re-probe every N seconds; print verdict TRANSITIONS only "
                   "(Ctrl-C to stop). Transitions also append to "
                   "~/.luxe/netwatch.log.")
def net_cmd(host: str | None, config_path: str | None, watch_s: int):
    """Layered network report: DNS → TCP → TLS → HTTP(S) + captive-portal
    check + every configured `backends:` endpoint. Deterministic (no model),
    every probe hard-bounded — total wall is a few seconds. The verdict names
    the broken LAYER (tls-blocked, captive-portal, dns-broken, …) instead of
    describing symptoms.
    """
    from luxe import netdiag

    try:
        cfg = _chat_cfg(config_path)
    except Exception:
        cfg = None
    anchor = host or netdiag.ANCHOR_HOST

    def _render(report) -> None:
        textfmt.render_ok_lines(console, netdiag.render_lines(report))
        style = "green" if report.ladder.verdict == netdiag.V_OK else "yellow"
        console.print(f"[{style}]verdict: {report.ladder.verdict}[/] — "
                      f"{report.ladder.advice}")

    report = netdiag.full_report(cfg, host=anchor)
    _render(report)
    if not watch_s:
        sys.exit(0 if report.ladder.verdict == netdiag.V_OK else 1)

    # Watch mode: the question on a bad network is "when does it change?"
    # (session 5bb630813c21: HTTPS silently recovered mid-flight). Quiet
    # while stable; a verdict transition prints + appends to the log.
    log_path = luxe_home() / "netwatch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    last = report.ladder.verdict
    console.print(f"[dim]watching every {watch_s}s — verdict transitions "
                  f"only (log: {log_path}) · Ctrl-C to stop[/]")
    try:
        while True:
            time.sleep(max(watch_s, 5))
            report = netdiag.full_report(cfg, host=anchor)
            now = report.ladder.verdict
            if now != last:
                stamp = time.strftime("%H:%M:%S")
                console.print(f"[bold]{stamp} {last} → {now}[/] — "
                              f"{report.ladder.advice}")
                try:
                    with log_path.open("a", encoding="utf-8") as fh:
                        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                                 f"{last} -> {now}\n")
                except OSError:
                    pass
                last = now
    except KeyboardInterrupt:
        console.print(f"[dim]stopped — last verdict: {last}[/]")
        sys.exit(0)


@main.command(name="planeproxy")
@click.option("--check", type=click.Choice(["status", "doctor", "both"]),
              default="both", help="Which probe to run (default: both)")
@click.option("--json", "as_json", is_flag=True,
              help="Emit the raw report as JSON instead of the rendered lines")
def planeproxy_cmd(check: str, as_json: bool):
    """Diagnose the planeproxy SSH tunnel (read-only). Runs its own
    `status --json` / `doctor --json` under a hard deadline and classifies
    the result into a verdict with the one fix that matters (host-key
    mismatch, captive portal, stranded routing, …). Never starts or stops
    the tunnel. Exit 0 when healthy, 1 otherwise.
    """
    from luxe import planeproxy as pp

    report = pp.full_report(check=check)
    if as_json:
        import dataclasses
        import json as json_mod
        click.echo(json_mod.dumps(dataclasses.asdict(report), indent=2))
    else:
        textfmt.render_ok_lines(console, pp.render_lines(report))
    sys.exit(0 if report.verdict == pp.PP_OK else 1)


@main.command(name="claudecode")
@click.option("--check", type=click.Choice(["status", "net", "all"]),
              default="all", help="Which probes to run (default: all)")
@click.option("--repo", default=None,
              help="Also inspect this project's .claude/settings*.json")
@click.option("--json", "as_json", is_flag=True,
              help="Emit the raw report as JSON instead of the rendered lines")
def claudecode_cmd(check: str, repo: str | None, as_json: bool):
    """Diagnose Claude Code (the `claude` CLI), read-only.

    Answers the question a luxe fallback session gets asked: which billing
    path is each running session actually on — Max-subscription login or
    Platform API key — and what is overriding it (ANTHROPIC_BASE_URL, an
    `env:` block, an apiKeyHelper, Bedrock/Vertex). Also reports settings-file
    validity, the install, and metadata for the recent sessions.

    Environment variables are reported by NAME only and Keychain lookups are
    metadata-only, so no secret is ever exposed; conversation content is never
    read. Never launches, kills, or reconfigures Claude Code. Exit 0 when
    healthy, 1 otherwise.
    """
    from luxe import claudecode as cc

    report = cc.full_report(check=check, repo_path=repo)
    if as_json:
        import dataclasses
        import json as json_mod
        # asdict recurses into the netdiag LadderReport too (also a dataclass),
        # so the ladder rungs survive the JSON form.
        click.echo(json_mod.dumps(dataclasses.asdict(report), indent=2))
    else:
        textfmt.render_ok_lines(console, cc.render_lines(report))
    sys.exit(0 if report.verdict == cc.CC_OK else 1)


def _omlx_base_url_from_config() -> str:
    """The chat config's oMLX endpoint, falling back to the local default."""
    try:
        return _chat_cfg().omlx_base_url
    except Exception:
        return "http://127.0.0.1:8000"


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
        org_repo = source.ref.replace("/", "--")
        cache_dir = (Path.home() / ".cache" / "huggingface" / "hub"
                     / f"models--{org_repo}")
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
        console.print(f"[yellow]· store materialization failed ({e}) — the "
                      f"model is only in the HF cache; re-run "
                      f"`luxe pull {source.name} --from {Path.home()}/.cache/"
                      "huggingface/hub` to fix[/]")


@main.command(name="pr")
@click.argument("run_id")
@click.option("--push-only", is_flag=True, help="Only do the push step (no PR create)")
@click.option("--watch-ci", is_flag=True, help="Poll gh pr checks after create")
def pr_cmd(run_id: str, push_only: bool, watch_ci: bool):
    """Resume a partially-completed PR cycle by run_id."""
    from luxe import pr as pr_mod

    try:
        state = pr_mod.resume_pr(
            run_id, push_only=push_only, watch_ci=watch_ci,
            on_event=lambda kind, data: console.print(f"[dim]· pr {kind}: {data}[/]"),
        )
    except pr_mod.PRError as e:
        console.print(f"[red]✗ {e}[/]")
        sys.exit(5)

    if state.pr_url:
        console.print(f"[bold green]✓ PR ready:[/] {state.pr_url}"
                      f" {'(draft)' if state.is_draft else ''}")
    else:
        console.print("[green]✓ Resume complete[/] (no PR created)")


@main.command(name="serve")
@click.option("--transport", default="stdio",
              type=click.Choice(["stdio", "sse"]),
              help="MCP transport (stdio for Claude Desktop subprocess; "
                   "sse for HTTP)")
@click.option("--port", default=8765, help="Port for sse transport")
@click.option("--unsafe", is_flag=True,
              help="Expose luxe_maintain (writes files, opens PRs). "
                   "Requires LUXE_MCP_UNSAFE=1 and LUXE_MCP_TOKEN env vars; "
                   "callers must pass a matching confirm_token.")
def serve_cmd(transport: str, port: int, unsafe: bool):
    """Run luxe as an MCP server (read-only by default)."""
    from luxe.mcp.server import build_server, load_server_policy, server_tool_names

    policy = load_server_policy()

    def _readonly_runner(tool_name: str, args: dict) -> str:
        repo_path = args.get("repo_path", "")
        goal = args.get("goal", "") or args.get("query", "")
        task_type = {"luxe_review": "review", "luxe_summarize": "summarize",
                     "luxe_explain": "summarize"}.get(tool_name, "review")
        return _run_pipeline_readonly(repo_path, goal, task_type)

    def _maintain_runner(args: dict) -> str:
        return _run_pipeline_maintain(args["repo_path"], args["goal"])

    server = build_server(
        unsafe=unsafe, policy=policy,
        readonly_runner=_readonly_runner,
        maintain_runner=_maintain_runner if unsafe else None,
    )

    tool_list = server_tool_names(unsafe, policy)
    sys.stderr.write(
        f"luxe serve: transport={transport} unsafe={unsafe} "
        f"tools={tool_list}\n"
    )
    sys.stderr.flush()

    if transport == "stdio":
        server.run(transport="stdio")
    elif transport == "sse":
        server.run(transport="sse")
    else:
        sys.stderr.write(f"unknown transport: {transport}\n")
        sys.exit(1)


def _run_pipeline_readonly(repo_path: str, goal: str, task_type: str) -> str:
    """Helper: drive a mono-mode pipeline with mutation tools stripped."""
    from luxe.agents.single import run_single
    from luxe.backend import Backend
    from luxe.mcp.server import make_read_only_role
    from luxe.tools.fs import set_repo_root

    repo_path = _resolve_repo(repo_path)
    set_repo_root(repo_path)
    cfg = load_config(None)
    role_cfg = make_read_only_role(cfg.role("monolith"))
    backend = Backend(base_url=cfg.omlx_base_url, model=cfg.model_for_role("monolith"))
    languages = _detect_languages_for_repo(repo_path)
    result = run_single(
        backend, role_cfg,
        goal=goal, task_type=task_type, languages=languages,
    )
    return result.final_text or "(no report produced)"


def _run_pipeline_maintain(repo_path: str, goal: str) -> str:
    """Helper: drive a full maintain pipeline. ONLY invoked when --unsafe."""
    from luxe.agents.single import run_single
    from luxe.backend import Backend
    from luxe.tools.fs import set_repo_root

    repo_path = _resolve_repo(repo_path)
    set_repo_root(repo_path)
    cfg = load_config(None)
    backend = Backend(base_url=cfg.omlx_base_url, model=cfg.model_for_role("monolith"))
    languages = _detect_languages_for_repo(repo_path)
    result = run_single(
        backend, cfg.role("monolith"),
        goal=goal, task_type="implement", languages=languages,
    )
    return result.final_text or "(no report produced)"


@main.group(name="runs")
def runs_group():
    """Manage luxe run state."""


@runs_group.command(name="list")
def runs_list_cmd():
    """List all known luxe runs (most recent first)."""
    from luxe.run_state import list_runs
    from luxe.pr import _first_incomplete  # type: ignore
    from luxe.run_state import load_pr_state

    runs = list_runs()
    if not runs:
        console.print("[dim]No runs found.[/]")
        return
    console.print(f"\n[bold]luxe runs[/]  ({len(runs)} total)")
    for spec in sorted(runs, key=lambda s: s.started_at, reverse=True)[:50]:
        prs = load_pr_state(spec.run_id)
        next_step = _first_incomplete(prs) if prs else "(no pr_state)"
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(spec.started_at))
        console.print(f"  [cyan]{spec.run_id}[/]  {when}  "
                      f"{spec.task_type}  "
                      f"[dim]{spec.goal[:60]}[/]  next:[yellow]{next_step}[/]")


@runs_group.command(name="gc")
@click.option("--days", default=7, help="Retention window (default 7 days)")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without deleting")
def runs_gc_cmd(days: int, dry_run: bool):
    """Remove run directories older than --days."""
    from luxe.run_state import gc_runs, list_runs

    if dry_run:
        cutoff = time.time() - (days * 86400)
        old = [s for s in list_runs() if s.started_at < cutoff]
        console.print(f"Would remove {len(old)} runs older than {days} days:")
        for s in old:
            console.print(f"  {s.run_id}  {time.strftime('%Y-%m-%d', time.localtime(s.started_at))}")
        return
    n = gc_runs(retention_days=days)
    console.print(f"[green]Removed {n} runs older than {days} days.[/]")





@main.command()
@click.option("--config", "config_path", default=None, help="Path to config YAML")
def check(config_path: str | None):
    """Check oMLX connectivity and model availability."""
    from luxe.backend import Backend

    config = load_config(config_path)
    backend = Backend(base_url=config.omlx_base_url)

    if not backend.health():
        console.print(f"[red]Cannot reach oMLX at {config.omlx_base_url}[/]")
        console.print("[dim]Run `brew services start omlx` and re-run.[/]")
        sys.exit(1)

    console.print(f"[green]oMLX is healthy[/] at {config.omlx_base_url}")

    required = list(config.models.values())
    missing = backend.assert_models_available(required)

    available = set(backend.list_models())
    console.print(f"\nAvailable models ({len(available)}):")
    for m in sorted(available):
        console.print(f"  {m}")

    console.print("\nPipeline model requirements:")
    for role_name, model_id in config.models.items():
        found = model_id in available
        status = "[green]✓[/]" if found else "[red]✗[/]"
        console.print(f"  {status} {role_name}: {model_id}")

    if missing:
        console.print(f"\n[yellow]Missing models: {', '.join(missing)}[/]")
        console.print("[dim]Load them in oMLX before running.[/]")
        sys.exit(1)
    else:
        console.print("\n[green]All pipeline models available.[/]")


if __name__ == "__main__":
    main()
