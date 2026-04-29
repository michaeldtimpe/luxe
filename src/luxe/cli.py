"""CLI entry point for luxe."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

import click
from rich.console import Console

from luxe.config import load_config
from luxe.metrics.collector import collect, save_metrics
from luxe.metrics.report import print_run_summary
from luxe.pipeline.orchestrator import PipelineOrchestrator

console = Console()


def _resolve_repo(repo: str) -> str:
    """Resolve a repo argument to a local path. Clones if it's a URL."""
    p = Path(repo).expanduser().resolve()
    if p.is_dir():
        return str(p)

    if repo.startswith(("http://", "https://", "git@")):
        clone_dir = Path(tempfile.mkdtemp(prefix="luxe_"))
        console.print(f"[dim]Cloning {repo} → {clone_dir}[/]")
        result = subprocess.run(
            ["git", "clone", "--depth=1", repo, str(clone_dir)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            console.print(f"[red]Clone failed:[/] {result.stderr}")
            sys.exit(1)
        return str(clone_dir)

    console.print(f"[red]Not a directory or repo URL:[/] {repo}")
    sys.exit(1)


@click.group()
def main():
    """luxe — MLX-only repo maintainer."""
    pass


@main.command()
@click.argument("repo")
@click.argument("goal")
@click.option("--type", "task_type", default="review",
              type=click.Choice(["review", "implement", "bugfix", "document", "summarize", "manage"]),
              help="Task type determines pipeline shape")
@click.option("--config", "config_path", default=None, help="Path to pipeline.yaml")
@click.option("--output", "output_dir", default="./runs", help="Directory for metrics output")
@click.option("--save-report", is_flag=True, help="Save final report as markdown")
def run(repo: str, goal: str, task_type: str, config_path: str | None,
        output_dir: str, save_report: bool):
    """Run a luxe pipeline against a repository.

    REPO: Local path or git URL to clone.
    GOAL: What to accomplish (e.g., "review for security issues").
    """
    config = load_config(config_path)
    repo_path = _resolve_repo(repo)

    console.print(f"\n[bold]Swarm Pipeline[/]")
    console.print(f"Task: {task_type} | Repo: {repo_path}")
    console.print(f"Goal: {goal}\n")

    orch = PipelineOrchestrator(config)
    pipeline_run = orch.run(goal, task_type, repo_path)

    metrics = collect(pipeline_run)
    print_run_summary(pipeline_run, metrics)

    metrics_path = save_metrics(metrics, output_dir)
    console.print(f"\n[dim]Metrics saved: {metrics_path}[/]")

    if save_report and pipeline_run.final_report:
        report_path = Path(output_dir) / f"report_{pipeline_run.id}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(pipeline_run.final_report)
        console.print(f"[dim]Report saved: {report_path}[/]")

    if pipeline_run.final_report:
        console.print(f"\n{'='*60}")
        console.print(pipeline_run.final_report)


_WRITE_TASKS = {"implement", "bugfix", "document", "manage"}


@main.command()
@click.argument("repo")
@click.argument("goal")
@click.option("--mode", "mode_flag", default="auto",
              type=click.Choice(["auto", "single", "swarm"]),
              help="Execution mode (default: auto picks based on goal + repo size)")
@click.option("--task", "task_type", default=None,
              type=click.Choice(["review", "implement", "bugfix", "document", "summarize", "manage"]),
              help="Task type (default: auto-detected from goal)")
@click.option("--swarm-config", "swarm_config_path", default=None,
              help="Path to swarm config YAML (default: configs/swarm_64gb.yaml)")
@click.option("--single-config", "single_config_path", default=None,
              help="Path to single-mode config YAML (default: configs/single_64gb.yaml)")
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
def maintain(
    repo: str, goal: str, mode_flag: str, task_type: str | None,
    swarm_config_path: str | None, single_config_path: str | None,
    allow_dirty: bool, skip_confirm: bool, watch_ci: bool,
    output_dir: str, save_report: bool,
):
    """Run a luxe maintain pipeline against a repository.

    REPO: Local path or git URL to clone.
    GOAL: What to accomplish (e.g., "fix the off-by-one in pagination").

    Mode selection (when --mode is auto):
      1. Goal-keyword pre-classifier — "implement"/"refactor"/etc. → swarm;
         "review"/"summarize"/etc. → single.
      2. Source-byte fallback — repos with >500 KB of source go swarm.
    """
    from luxe.agents.single import did_escalate, run_single
    from luxe.backend import Backend
    from luxe.citations import lint_report
    from luxe.escalation import capture_from_single
    from luxe.locks import LockHeld, acquire_repo_lock
    from luxe.mode_select import RunMode, select_mode
    from luxe import pr as pr_mod
    from luxe.run_state import RunSpec, append_event, init_run_dir, run_dir
    from luxe.tools.fs import set_repo_root

    repo_path = _resolve_repo(repo)
    decision = select_mode(goal=goal, repo_root=Path(repo_path), override=mode_flag)
    detected_task = task_type or _infer_task_type(goal)

    # --- preflight (BEFORE acquiring the lock; cheap checks first) ----------
    confirm_callback: Callable[[], bool] | None
    if skip_confirm:
        confirm_callback = lambda: True
    elif sys.stdin.isatty():
        def _confirm() -> bool:
            click.echo(
                "Type 'yes' to continue with --allow-dirty. Your uncommitted "
                "changes WILL be included in the PR diff."
            )
            return click.prompt("→", default="", show_default=False).strip() == "yes"
        confirm_callback = _confirm
    else:
        confirm_callback = None

    pr_cfg = pr_mod.load_pr_config()
    try:
        prep = pr_mod.preflight(
            repo_path,
            task_type=detected_task,
            goal=goal,
            allow_dirty=allow_dirty,
            confirm_callback=confirm_callback,
            cfg=pr_cfg,
        )
    except pr_mod.GhAuthError as e:
        console.print(f"[red]✗ {e}[/]")
        sys.exit(2)
    except pr_mod.DirtyTreeError as e:
        console.print(f"[red]✗ {e}[/]")
        sys.exit(2)

    # --- run state & lock ---------------------------------------------------
    spec = RunSpec(
        goal=goal,
        mode=mode_flag,
        actual_mode=decision.mode.value,
        task_type=detected_task,
        repo_path=str(Path(repo_path).resolve()),
        base_sha=prep.base_sha,
        base_branch=prep.base_branch,
    )
    init_run_dir(spec)
    append_event(spec.run_id, "preflight_ok",
                 base_branch=prep.base_branch, branch_name=prep.branch_name,
                 test_command=prep.test_command, mode=decision.mode.value)

    console.print(f"\n[bold]luxe maintain[/]  [dim]run_id={spec.run_id}[/]")
    console.print(f"Repo: {repo_path}")
    console.print(f"Goal: {goal}")
    console.print(f"Mode: [cyan]{decision.mode.value}[/]  ([dim]{decision.reason}[/])")
    console.print(f"Task: {detected_task}")
    console.print(f"Branch: [dim]{prep.branch_name}[/]  Base: [dim]{prep.base_branch}@{prep.base_sha[:8]}[/]")
    if prep.test_command:
        console.print(f"Tests: [dim]{prep.test_command}[/]")
    else:
        console.print(f"Tests: [dim](none detected)[/]")

    try:
        ctx = acquire_repo_lock(spec.repo_path, spec.run_id)
        lock_path = ctx.__enter__()  # acquire
    except LockHeld as e:
        console.print(f"\n[red]✗ {e}[/]")
        sys.exit(3)

    # --- MCP client (opt-in via configs/mcp.yaml) -----------------------
    from luxe.mcp.client import MCPClientManager, load_mcp_config
    mcp_cfg = load_mcp_config()
    mcp_mgr: MCPClientManager | None = None
    extra_tool_defs: list = []
    extra_tool_fns: dict = {}
    if mcp_cfg.servers:
        mcp_mgr = MCPClientManager(mcp_cfg).start()
        extra_tool_defs, extra_tool_fns = mcp_mgr.discover_tools(
            only_for_task=detected_task,
        )
        if extra_tool_defs:
            console.print(f"[dim]· MCP: {len(extra_tool_defs)} tool(s) "
                          f"from {len([s for s in mcp_mgr.server_status() if not s['down']])} "
                          f"server(s)[/]")
        for s in mcp_mgr.server_status():
            if s["down"]:
                console.print(f"[yellow]· MCP server {s['name']} DOWN: "
                              f"{s['down_reason']}[/]")

    try:
        # --- pipeline -------------------------------------------------------
        pipeline_run = None
        if decision.mode == RunMode.SINGLE:
            cfg = load_config(single_config_path or _default_single_config())
            set_repo_root(repo_path)
            backend = Backend(base_url=cfg.omlx_base_url, model=cfg.model_for_role("monolith"))
            languages = _detect_languages_for_repo(repo_path)

            console.print(f"\n[bold cyan]▶ Single mode[/]  (model: {cfg.model_for_role('monolith')})")
            single_result = run_single(
                backend, cfg.role("monolith"),
                goal=goal,
                task_type=detected_task,
                languages=languages,
                extra_tool_defs=extra_tool_defs or None,
                extra_tool_fns=extra_tool_fns or None,
            )

            if did_escalate(single_result):
                console.print("[yellow]↑ Single mode escalated to swarm[/]  "
                              f"({single_result.tool_calls_total} tool calls)")
                esc_ctx = capture_from_single(
                    single_result.tool_calls,
                    final_text=single_result.final_text,
                    abort_reason="single-mode escalated to swarm",
                )
                swarm_cfg = load_config(swarm_config_path)
                orch = PipelineOrchestrator(
                    swarm_cfg, run_id=spec.run_id,
                    extra_tool_defs=extra_tool_defs or None,
                    extra_tool_fns=extra_tool_fns or None,
                )
                pipeline_run = orch.run(goal, detected_task, repo_path,
                                        initial_context=esc_ctx.render())
                final_report = pipeline_run.final_report or ""
                spec.actual_mode = "swarm"  # record the actual path taken
            else:
                final_report = single_result.final_text or ""
        else:
            cfg = load_config(swarm_config_path)
            orch = PipelineOrchestrator(
                cfg, run_id=spec.run_id,
                extra_tool_defs=extra_tool_defs or None,
                extra_tool_fns=extra_tool_fns or None,
            )
            pipeline_run = orch.run(goal, detected_task, repo_path)
            final_report = pipeline_run.final_report or ""

        # Persist the synthesizer/single report so resume_pr can find it.
        if final_report:
            (run_dir(spec.run_id) / "synthesizer.md").write_text(final_report)

        # --- citation lint --------------------------------------------------
        envelope = pipeline_run.validator_envelope if pipeline_run else None
        if final_report:
            lint = lint_report(final_report, repo_path, base_sha=prep.base_sha,
                               envelope=envelope)
            if lint.is_blocking:
                console.print(f"\n[red]✗ Citation lint failed[/] — "
                              f"{len(lint.unresolved)} unresolved: {lint.summary()}")
                for r in lint.unresolved[:10]:
                    console.print(f"    - `{r.citation.path}:{r.citation.line}` — "
                                  f"[red]{r.status}[/]: {r.detail}")
                append_event(spec.run_id, "citation_lint_blocked",
                             unresolved=len(lint.unresolved), summary=lint.summary())
            else:
                console.print(f"\n[green]✓ Citation lint passed[/] "
                              f"({len(lint.citations)} citations: {lint.summary()})")
                append_event(spec.run_id, "citation_lint_passed",
                             count=len(lint.citations), summary=lint.summary())

        # --- PR cycle for write-tasks --------------------------------------
        if detected_task in _WRITE_TASKS:
            try:
                pr_state = pr_mod.open_pr(
                    spec,
                    report_text=final_report,
                    task_type=detected_task,
                    goal=goal,
                    test_command=prep.test_command,
                    branch_name=prep.branch_name,
                    cfg=pr_cfg,
                    watch_ci=watch_ci,
                    on_event=lambda kind, data: console.print(
                        f"[dim]· pr {kind}: {data}[/]"
                    ),
                )
                if pr_state.pr_url:
                    console.print(f"\n[bold green]✓ PR opened:[/] {pr_state.pr_url}"
                                  f" {'(draft)' if pr_state.is_draft else ''}")
                else:
                    console.print(f"\n[yellow]· No PR opened (no diff produced)[/]")
            except pr_mod.NoMutationsError as e:
                console.print(f"\n[red]✗ {e}[/]")
                console.print(f"[dim]Status: failed_no_mutations_produced. "
                              f"Resume not applicable.[/]")
                sys.exit(4)
            except pr_mod.PRError as e:
                console.print(f"\n[red]✗ PR cycle blocked: {e}[/]")
                console.print(f"[dim]Resume with: luxe pr {spec.run_id}[/]")
                sys.exit(5)
        elif detected_task in {"review", "summarize"}:
            console.print(f"\n[dim](read-only task; no PR)[/]")

        # --- optional save-report --------------------------------------------
        if save_report and final_report:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            report_path = out / f"report_{spec.run_id}.md"
            report_path.write_text(final_report)
            console.print(f"[dim]Report also saved: {report_path}[/]")

        if final_report:
            console.print(f"\n{'='*60}")
            console.print(final_report)
    finally:
        if mcp_mgr is not None:
            try:
                mcp_mgr.close()
            except Exception:
                pass
        try:
            ctx.__exit__(None, None, None)  # release lock
        except Exception:
            pass


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
        console.print(f"[green]✓ Resume complete[/] (no PR created)")


@main.command(name="resume")
@click.argument("run_id")
@click.option("--force-resume", is_flag=True,
              help="Resume even if HEAD has moved since the checkpoint "
                   "(invalidates cached worker findings).")
@click.option("--allow-dirty", is_flag=True, help="Permit a dirty working tree on resume")
@click.option("--yes", "skip_confirm", is_flag=True, help="Skip TTY confirmations")
@click.option("--watch-ci", is_flag=True, help="Poll gh pr checks after the PR is opened")
def resume_cmd(run_id: str, force_resume: bool, allow_dirty: bool,
               skip_confirm: bool, watch_ci: bool):
    """Resume a paused/failed luxe maintain run from its last completed stage.

    Architect / worker / validator / synthesizer outputs are loaded from
    ~/.luxe/runs/<run-id>/stages/ when present; only stages without a
    checkpoint are re-run. The PR cycle resumes via pr.py with the same
    checkpointed step ledger.

    By default, the run is rejected if HEAD has moved since the checkpoint
    was created — cached worker findings may not match current state. Pass
    `--force-resume` to clear the stage cache and re-run from scratch
    while keeping the original RunSpec (goal, mode, branch name, etc.).
    """
    from luxe.locks import LockHeld, acquire_repo_lock
    from luxe import pr as pr_mod
    from luxe.run_state import (
        append_event,
        clear_stages,
        list_completed_stages,
        load_run_spec,
        run_dir,
    )

    spec = load_run_spec(run_id)
    if spec is None:
        console.print(f"[red]✗ unknown run_id {run_id}[/]")
        sys.exit(1)

    # Drift detection (Reviewer R2.1 round 2)
    current = _git_head_sha(spec.repo_path)
    drifted = current and current != spec.base_sha
    if drifted and not force_resume:
        console.print(
            f"\n[red]✗ Repo has changed since checkpoint[/]\n"
            f"  base_sha: {spec.base_sha[:12]}\n"
            f"  current : {current[:12]}\n"
            f"Cached worker findings may not match the current code. Re-run "
            f"from scratch, or pass `--force-resume` to invalidate the cache "
            f"and resume with the same RunSpec (goal, mode, branch_name)."
        )
        sys.exit(6)
    if drifted and force_resume:
        n = clear_stages(run_id)
        console.print(f"[yellow]· Stage cache invalidated ({n} files)[/] — "
                      f"resuming with fresh worker pass")
        append_event(run_id, "resume_with_drift", removed=n,
                     base_sha=spec.base_sha[:12], current=current[:12])

    # Pre-flight (lighter than fresh run — branch name comes from pr_state)
    confirm_callback: Callable[[], bool] | None = None
    if skip_confirm:
        confirm_callback = lambda: True
    elif sys.stdin.isatty():
        def _confirm() -> bool:
            click.echo("Type 'yes' to continue with --allow-dirty.")
            return click.prompt("→", default="", show_default=False).strip() == "yes"
        confirm_callback = _confirm

    try:
        pr_mod.assert_gh_auth()
        pr_mod.assert_clean_tree(spec.repo_path, allow_dirty=allow_dirty,
                                 confirm_callback=confirm_callback)
    except pr_mod.GhAuthError as e:
        console.print(f"[red]✗ {e}[/]")
        sys.exit(2)
    except pr_mod.DirtyTreeError as e:
        console.print(f"[red]✗ {e}[/]")
        sys.exit(2)

    completed = list_completed_stages(run_id)
    console.print(f"\n[bold]luxe resume[/]  [dim]run_id={run_id}[/]")
    console.print(f"Goal: {spec.goal}")
    console.print(f"Mode: {spec.actual_mode or spec.mode}")
    console.print(f"Stages on disk: {', '.join(completed) or '(none)'}")

    try:
        ctx = acquire_repo_lock(spec.repo_path, spec.run_id)
        ctx.__enter__()
    except LockHeld as e:
        console.print(f"\n[red]✗ {e}[/]")
        sys.exit(3)

    try:
        # Re-run pipeline (cached stages skipped automatically by checkpoints)
        # Single-mode resume not supported in v1.0 — single mode crashes are
        # cheap to redo (one model, <30 turns); only swarm runs need stage-level
        # resume for the 40-min jobs.
        if (spec.actual_mode or spec.mode) == "single":
            console.print("[yellow]· Single-mode resume is not supported "
                          "(re-run from scratch instead).[/]")
            sys.exit(7)

        from luxe.tools.fs import set_repo_root
        set_repo_root(spec.repo_path)
        cfg = load_config(None)  # default swarm config
        orch = PipelineOrchestrator(cfg, run_id=spec.run_id)
        pipeline_run = orch.run(
            spec.goal, spec.task_type, spec.repo_path,
        )
        final_report = pipeline_run.final_report or ""

        # Persist report copy for downstream pr.py resume.
        if final_report:
            (run_dir(spec.run_id) / "synthesizer.md").write_text(final_report)

        # Citation lint
        if final_report:
            from luxe.citations import lint_report
            lint = lint_report(final_report, spec.repo_path,
                               base_sha=spec.base_sha,
                               envelope=pipeline_run.validator_envelope)
            if lint.is_blocking:
                console.print(f"\n[red]✗ Citation lint failed[/] — "
                              f"{len(lint.unresolved)} unresolved: {lint.summary()}")
            else:
                console.print(f"\n[green]✓ Citation lint passed[/] "
                              f"({len(lint.citations)} citations)")

        # PR cycle if applicable
        if spec.task_type in _WRITE_TASKS:
            try:
                pr_state = pr_mod.resume_pr(
                    spec.run_id, watch_ci=watch_ci,
                    on_event=lambda kind, data: console.print(
                        f"[dim]· pr {kind}: {data}[/]"
                    ),
                )
                if pr_state.pr_url:
                    console.print(f"\n[bold green]✓ PR ready:[/] {pr_state.pr_url}"
                                  f" {'(draft)' if pr_state.is_draft else ''}")
            except pr_mod.PRError as e:
                console.print(f"\n[red]✗ PR cycle blocked: {e}[/]")
                console.print(f"[dim]Resume with: luxe pr {spec.run_id}[/]")
                sys.exit(5)
    finally:
        try:
            ctx.__exit__(None, None, None)
        except Exception:
            pass


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
                      f"{spec.actual_mode or spec.mode}/{spec.task_type}  "
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


def _default_single_config() -> str:
    return str(Path(__file__).parent.parent.parent / "configs" / "single_64gb.yaml")


def _git_head_sha(repo_path: str) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path,
            capture_output=True, text=True, check=False,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except OSError:
        return ""


def _infer_task_type(goal: str) -> str:
    g = goal.lower()
    if any(k in g for k in ("implement", "add ", "build", "create", "introduce")):
        return "implement"
    if any(k in g for k in ("fix", "bug", "broken", "regression")):
        return "bugfix"
    if any(k in g for k in ("document", "docs", "readme", "docstring")):
        return "document"
    if any(k in g for k in ("update deps", "upgrade", "ci", "config")):
        return "manage"
    if any(k in g for k in ("summarize", "summary", "explain", "describe")):
        return "summarize"
    return "review"


def _detect_languages_for_repo(repo_path: str) -> frozenset[str]:
    p = Path(repo_path)
    lang_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".tsx": "typescript", ".jsx": "javascript", ".rs": "rust",
        ".go": "go",
    }
    found: set[str] = set()
    import os as _os
    for root, dirs, files in _os.walk(p):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"}]
        for f in files:
            ext = Path(f).suffix.lower()
            if ext in lang_map:
                found.add(lang_map[ext])
    return frozenset(found)


@main.command()
@click.option("--config", "config_path", default=None, help="Path to swarm_64gb.yaml")
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

    console.print(f"\nPipeline model requirements:")
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


@main.command()
@click.argument("metrics_dir", default="./runs")
def compare(metrics_dir: str):
    """Compare metrics from multiple pipeline runs."""
    from luxe.metrics.collector import RunMetrics
    from luxe.metrics.report import print_comparison

    p = Path(metrics_dir)
    if not p.is_dir():
        console.print(f"[red]Directory not found:[/] {metrics_dir}")
        sys.exit(1)

    runs: list[tuple[str, RunMetrics]] = []
    for f in sorted(p.glob("run_*.json")):
        data = json.loads(f.read_text())
        m = RunMetrics(**{k: v for k, v in data.items()
                         if k in RunMetrics.__dataclass_fields__})
        label = f"{m.task_type}/{m.run_id[:8]}"
        runs.append((label, m))

    if not runs:
        console.print("[yellow]No run metrics found.[/]")
        sys.exit(1)

    print_comparison(runs)


@main.command()
@click.argument("configs", nargs=-1, required=True)
@click.option("--tasks", "task_ids", multiple=True, help="Specific task IDs to run")
@click.option("--tags", multiple=True, help="Filter tasks by tag (security, python, core, etc.)")
@click.option("--output", "output_dir", default="./benchmarks", help="Output directory")
@click.option("--fixtures", "fixture_dir", default=None, help="Directory for test repos (default: temp)")
def benchmark(configs: tuple[str, ...], task_ids: tuple[str, ...], tags: tuple[str, ...],
              output_dir: str, fixture_dir: str | None):
    """Run benchmark tasks across multiple pipeline configs.

    CONFIGS: One or more paths to pipeline YAML configs.

    Examples:
        luxe benchmark configs/qwen_32gb.yaml configs/deepseek_32gb.yaml
        luxe benchmark configs/*.yaml --tags security --output ./results
        luxe benchmark configs/qwen_32gb.yaml --tasks review-python-security
    """
    from luxe.benchmark.compare import print_suite_summary
    from luxe.benchmark.runner import run_benchmark

    suite = run_benchmark(
        config_paths=list(configs),
        task_ids=list(task_ids) or None,
        task_tags=list(tags) or None,
        output_dir=output_dir,
        fixture_dir=fixture_dir,
    )

    console.print(f"\n{'='*60}")
    console.print("[bold]BENCHMARK RESULTS[/]")
    console.print(f"{'='*60}\n")

    print_suite_summary(suite)


@main.command(name="benchmark-report")
@click.argument("suite_path")
def benchmark_report(suite_path: str):
    """Print results from a previously saved benchmark suite.

    SUITE_PATH: Path to a bench_*.json file from a previous benchmark run.
    """
    from luxe.benchmark.compare import load_suite, print_suite_summary

    suite = load_suite(suite_path)
    print_suite_summary(suite)


@main.command(name="list-tasks")
@click.option("--tags", multiple=True, help="Filter by tag")
def list_tasks(tags: tuple[str, ...]):
    """List available benchmark tasks."""
    from luxe.benchmark.tasks import get_tasks

    tasks = get_tasks(list(tags) or None)
    if not tasks:
        console.print("[yellow]No tasks found.[/]")
        return

    table_data = []
    for t in tasks:
        console.print(f"  [cyan]{t.id}[/] — {t.name}")
        console.print(f"    Type: {t.task_type} | Fixture: {t.fixture} | Tags: {', '.join(t.tags)}")
        gt = t.ground_truth
        if gt.expected_findings:
            console.print(f"    Expected findings: {len(gt.expected_findings)}")


@main.command(name="list-models")
@click.argument("config_path")
def list_models(config_path: str):
    """Show all models required by a pipeline config with memory estimates."""
    config = load_config(config_path)

    console.print(f"\n[bold]Models for: {config_path}[/]\n")

    seen: dict[str, list[str]] = {}
    for role_name, model_id in config.models.items():
        if model_id not in seen:
            seen[model_id] = []
        seen[model_id].append(role_name)

    for model_id, roles in seen.items():
        console.print(f"  [cyan]{model_id}[/]")
        console.print(f"    Roles: {', '.join(roles)}")

    console.print(f"\n  Unique models: {len(seen)}")
    console.print(f"  (Pipeline is sequential — only one model loaded at a time)")


@main.command(name="benchmark-repos")
@click.argument("repos", nargs=-1, required=True)
@click.option("--configs", "-c", multiple=True, required=True,
              help="Pipeline config paths (pass multiple for comparison)")
@click.option("--output", "output_dir", default="./benchmarks/real", help="Output directory")
@click.option("--clone-dir", default=None, help="Where to clone repos (default: temp dir)")
@click.option("--tasks", "task_filter", multiple=True,
              type=click.Choice(["summarize", "review", "manage"]),
              help="Which tasks to run (default: all three)")
def benchmark_repos(repos: tuple[str, ...], configs: tuple[str, ...],
                    output_dir: str, clone_dir: str | None,
                    task_filter: tuple[str, ...]):
    """Run real-world benchmarks against GitHub repos.

    REPOS: One or more GitHub URLs or local paths.

    Runs each repo through summarize, review, and improvement-suggestion tasks
    with every config, then prints a head-to-head comparison.

    Examples:

      luxe benchmark-repos https://github.com/user/repo1 https://github.com/user/repo2 \\
        -c configs/qwen_32gb.yaml -c configs/deepseek_32gb.yaml

      luxe benchmark-repos /path/to/local/repo \\
        -c configs/qwen_32gb.yaml --tasks review --tasks summarize
    """
    from luxe.benchmark.real_world import (
        RepoSpec, RepoTask, DEFAULT_TASKS,
        run_real_world_benchmark, print_real_world_comparison,
    )

    repo_specs = [RepoSpec(url=url) for url in repos]

    if task_filter:
        for spec in repo_specs:
            spec.tasks = [t for t in DEFAULT_TASKS if t.task_type in task_filter]

    suite = run_real_world_benchmark(
        config_paths=list(configs),
        repos=repo_specs,
        output_dir=output_dir,
        clone_dir=clone_dir,
    )

    console.print(f"\n{'='*70}")
    console.print("[bold]REAL-WORLD BENCHMARK RESULTS[/]")
    console.print(f"{'='*70}\n")

    print_real_world_comparison(suite)


@main.command(name="benchmark-repos-report")
@click.argument("suite_path")
def benchmark_repos_report(suite_path: str):
    """Print results from a saved real-world benchmark suite.

    SUITE_PATH: Path to a real_*.json file.
    """
    from luxe.benchmark.real_world import load_real_world_suite, print_real_world_comparison

    suite = load_real_world_suite(suite_path)
    print_real_world_comparison(suite)


if __name__ == "__main__":
    main()
