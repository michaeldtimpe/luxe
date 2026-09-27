"""Pipeline + bookkeeping commands: `maintain`, `compare`, `pr`, `serve`,
`runs`, `check`.

`benchmarks/maintain_suite/run.py` and `benchmarks/swebench/adapter.py` spawn
`python -m luxe.cli maintain`, so the `maintain` command, its arguments, and
its options are a contract (luxe.sdd).
"""

from __future__ import annotations

import sys
import time

import click

from luxe.cli._common import (
    _chat_cfg,
    _infer_task_type,
    _resolve_repo,
    _unload_unless,
    console,
    main,
)
from luxe.config import load_config
from luxe.maintain import (  # noqa: F401  (shell + re-exports)
    _default_config,
    _diff_against_base,
    _should_reprompt_for_under_engagement,
    _WRITE_TASKS,
    maintain_pipeline,
)
from luxe.repo_index import _detect_languages_for_repo


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
        _unload_unless(keep_loaded, cfg)


@compare_group.command(name="review")
@click.argument("compare_id", required=False, default="")
def compare_review_cmd(compare_id):
    """Replay a stored comparison and tally its votes (no arg: list them)."""
    from luxe.compare import store
    store.review(compare_id, console=console)


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
    where = f" port={port}" if transport == "sse" else ""
    sys.stderr.write(
        f"luxe serve: transport={transport}{where} unsafe={unsafe} "
        f"tools={tool_list}\n"
    )
    sys.stderr.flush()

    if transport == "stdio":
        server.run(transport="stdio")
    elif transport == "sse":
        # FastMCP binds settings.port, whose default (8000) is the local
        # oMLX endpoint — `--port` was accepted and silently ignored.
        server.settings.port = port
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
