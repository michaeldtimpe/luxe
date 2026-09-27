"""gitkit commands: `gitaudit`, `gitchange`, `gitapply` (their hidden
back-compat aliases are registered in `luxe.cli.__init__`), plus `init`,
which drafts the gitkit orientation brief into `.luxe/memory.md`."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from luxe.cli._common import (
    _chat_cfg,
    _resolve_repo,
    _unload_unless,
    console,
    main,
)


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
        _unload_unless(keep_loaded, cfg)


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
        _unload_unless(keep_loaded, cfg)
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
