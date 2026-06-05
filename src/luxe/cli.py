"""CLI entry point for luxe — mono-only execution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

import click
from rich.console import Console

from luxe.config import load_config

console = Console()


def _resolve_repo(repo: str, *, full_history: bool = False) -> str:
    """Resolve a repo argument to a local path. Clones if it's a URL.

    `full_history=True` clones with `--filter=blob:none` (full commit history,
    lazy blobs) so commit-cadence/health analysis is meaningful; the default
    `--depth=1` shallow clone is fine for code-only analysis.
    """
    p = Path(repo).expanduser().resolve()
    if p.is_dir():
        return str(p)

    if repo.startswith(("http://", "https://", "git@")):
        clone_dir = Path(tempfile.mkdtemp(prefix="luxe_"))
        console.print(f"[dim]Cloning {repo} → {clone_dir}[/]")
        clone_args = (["--filter=blob:none"] if full_history else ["--depth=1"])
        result = subprocess.run(
            ["git", "clone", *clone_args, repo, str(clone_dir)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            console.print(f"[red]Clone failed:[/] {result.stderr}")
            sys.exit(1)
        return str(clone_dir)

    console.print(f"[red]Not a directory or repo URL:[/] {repo}")
    sys.exit(1)


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


_WRITE_TASKS = {"implement", "bugfix", "document", "manage"}


# v1.3 probe: re-prompt-on-under-engagement lever for doc tasks. The B1+B2
# overlay attempts (v1.1 abstract / v1.2 procedural anchor) both failed to
# unblock lpe-typing's under-engagement at the model scale. This is a
# runtime lever instead: after the agent loop finishes, if a doc-task diff
# is suspiciously small, re-invoke the agent with the goal + actual diff
# and a directive to find missing deliverables. Hardcoded threshold for
# the probe; if the lever lands, promote to RoleConfig.
_REPROMPT_DOC_ADDITIONS_THRESHOLD = 10


def _diff_against_base(repo_path: str, base_sha: str) -> tuple[int, int, str]:
    """Return (additions, deletions, diff_text) of working tree vs base_sha.

    Mark untracked files as intent-to-add (`git add -N`) before diffing.
    Without this, `git diff <base_sha>` only shows changes to tracked
    files — newly created files (e.g., write_file('CONFIG.md', ...))
    are invisible until staged. Intent-to-add adds an index entry without
    staging content, which is enough for diff to surface the new file
    as a +N/-0 change. The PR cycle's later `git add . && git commit`
    still works correctly.
    """
    subprocess.run(
        ["git", "add", "-N", "."],
        cwd=repo_path, capture_output=True, text=True,
    )
    additions = deletions = 0
    stat = subprocess.run(
        ["git", "diff", "--numstat", base_sha, "--"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if stat.returncode == 0:
        for line in stat.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                try:
                    additions += int(parts[0])
                    deletions += int(parts[1])
                except ValueError:
                    pass
    patch = subprocess.run(
        ["git", "diff", base_sha, "--"],
        cwd=repo_path, capture_output=True, text=True,
    )
    diff_text = patch.stdout if patch.returncode == 0 else ""
    return additions, deletions, diff_text


def _should_reprompt_for_under_engagement(task_type: str, additions: int) -> bool:
    """Reprompt gate: doc tasks with diff additions below threshold.

    Validated v1.3.0 on `nothing-ever-happens-document-config` (3/3 PASS as
    variance stabilizer; baseline 2/3). Set LUXE_REPROMPT_ON_DOC=1 to
    enable. Kept opt-in until a wider doc-fixture validation (n≥3 fixtures
    where reprompt actually fires) lands — n=1 fixture × 3 reps is enough
    to ship the lever, not enough to default-promote it.
    """
    if os.environ.get("LUXE_REPROMPT_ON_DOC") != "1":
        return False
    return task_type == "document" and additions < _REPROMPT_DOC_ADDITIONS_THRESHOLD


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
    from luxe.agents.single import run_single
    from luxe.backend import Backend
    from luxe.citations import lint_report
    from luxe.locks import LockHeld, acquire_repo_lock
    from luxe import pr as pr_mod
    from luxe.run_state import RunSpec, append_event, init_run_dir, run_dir
    from luxe.tools.fs import set_repo_root

    repo_path = _resolve_repo(repo)
    detected_task = task_type or _infer_task_type(goal)

    # SpecDD Lever 1 (v1.4-prep): load spec from --spec-yaml if provided.
    # Failed loads (missing file, malformed YAML, invalid spec) abort the
    # run BEFORE the model is loaded so the user sees the error fast.
    # When None, the reprompt block falls back to v1.3 directive behavior.
    loaded_spec = None
    if spec_yaml_path:
        import yaml as _yaml
        from luxe.spec import spec_from_yaml_dict
        with open(spec_yaml_path) as _f:
            loaded_spec = spec_from_yaml_dict(_yaml.safe_load(_f) or {})

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

    spec = RunSpec(
        goal=goal,
        task_type=detected_task,
        repo_path=str(Path(repo_path).resolve()),
        base_sha=prep.base_sha,
        base_branch=prep.base_branch,
    )
    init_run_dir(spec)
    append_event(spec.run_id, "preflight_ok",
                 base_branch=prep.base_branch, branch_name=prep.branch_name,
                 test_command=prep.test_command)

    console.print(f"\n[bold]luxe maintain[/]  [dim]run_id={spec.run_id}[/]")
    console.print(f"Repo: {repo_path}")
    console.print(f"Goal: {goal}")
    console.print(f"Task: {detected_task}")
    branch_display = prep.branch_name or "(none)"
    console.print(f"Branch: [dim]{branch_display}[/]  Base: [dim]{prep.base_branch}@{prep.base_sha[:8]}[/]")
    if prep.test_command:
        console.print(f"Tests: [dim]{prep.test_command}[/]")
    else:
        console.print(f"Tests: [dim](none detected)[/]")

    try:
        ctx = acquire_repo_lock(spec.repo_path, spec.run_id)
        ctx.__enter__()
    except LockHeld as e:
        console.print(f"\n[red]✗ {e}[/]")
        sys.exit(3)

    from luxe import search as search_mod
    from luxe import symbols as symbols_mod
    console.print("[dim]· Building BM25 + symbol indices…[/]")
    bm25 = search_mod.build_bm25_index(repo_path)
    sym_idx = symbols_mod.build_symbol_index(repo_path)
    search_mod.set_index(bm25)
    symbols_mod.set_index(sym_idx)
    console.print(f"[dim]  BM25: {len(bm25.paths)} files | "
                  f"symbols: {len(sym_idx.symbols)} symbols across "
                  f"{sorted(sym_idx.coverage)}[/]")

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
        cfg = load_config(config_path or _default_config())
        set_repo_root(repo_path)
        backend = Backend(base_url=cfg.omlx_base_url, model=cfg.model_for_role("monolith"))
        languages = _detect_languages_for_repo(repo_path)

        console.print(f"\n[bold cyan]▶ Mono mode[/]  (model: {cfg.model_for_role('monolith')})")
        single_result = run_single(
            backend, cfg.role("monolith"),
            goal=goal,
            task_type=detected_task,
            languages=languages,
            extra_tool_defs=extra_tool_defs or None,
            extra_tool_fns=extra_tool_fns or None,
            run_id=spec.run_id,
            phase="main",
        )
        append_event(spec.run_id, "single_mode_done",
                     wall_s=single_result.wall_s,
                     prompt_tokens=single_result.prompt_tokens,
                     completion_tokens=single_result.completion_tokens,
                     tool_calls_total=single_result.tool_calls_total,
                     schema_rejects=single_result.schema_rejects,
                     aborted=single_result.aborted,
                     abort_reason=single_result.abort_reason,
                     final_text_chars=len(single_result.final_text or ""),
                     peak_context_pressure=single_result.peak_context_pressure)
        if detected_task in _WRITE_TASKS:
            _ds = _diff_against_base(repo_path, prep.base_sha)
            append_event(spec.run_id, "diff_stat",
                         checkpoint="after_main_pass",
                         additions=_ds[0], deletions=_ds[1])

        final_report = single_result.final_text or ""

        if final_report:
            (run_dir(spec.run_id) / "synthesizer.md").write_text(final_report)

        if final_report:
            lint = lint_report(final_report, repo_path, base_sha=prep.base_sha,
                               envelope=None)
            if lint.is_blocking:
                console.print(f"\n[red]✗ Lint failed[/] — "
                              f"{len(lint.unresolved)} unresolved citation(s), "
                              f"{len(lint.spec_violations)} spec violation(s): "
                              f"{lint.summary()}")
                for r in lint.unresolved[:10]:
                    console.print(f"    - `{r.citation.path}:{r.citation.line}` — "
                                  f"[red]{r.status}[/]: {r.detail}")
                for sv in lint.spec_violations[:10]:
                    console.print(
                        f"    - [red]spec_violation[/] `{sv.path}` "
                        f"forbidden by `{sv.sdd_path}` (glob `{sv.glob}`)"
                    )
                append_event(spec.run_id, "citation_lint_blocked",
                             unresolved=len(lint.unresolved),
                             spec_violations=len(lint.spec_violations),
                             summary=lint.summary())
            else:
                console.print(f"\n[green]✓ Lint passed[/] "
                              f"({len(lint.citations)} citations: {lint.summary()})")
                append_event(spec.run_id, "citation_lint_passed",
                             count=len(lint.citations), summary=lint.summary())
            # Orphans are warning-only at Lever 2 — surface them for human
            # visibility but do not block the run.
            if lint.spec_orphans:
                console.print(
                    f"[yellow]· {len(lint.spec_orphans)} spec_orphan warning(s)[/]"
                )
                for so in lint.spec_orphans[:5]:
                    console.print(f"    - `{so.path}` (no Owns: glob covers this path)")

        # SpecDD Lever 1 (v1.4-prep): when a spec is provided, the reprompt
        # gate uses per-requirement validation. Run validate() once and
        # short-circuit the v1.3 path entirely. Still gated by
        # LUXE_REPROMPT_ON_DOC=1 so the env-var contract is unchanged.
        _spec_validation = None
        if (loaded_spec is not None
                and detected_task in _WRITE_TASKS
                and os.environ.get("LUXE_REPROMPT_ON_DOC") == "1"):
            from luxe.spec_validator import (
                validate as _validate_spec,
                format_unsatisfied_for_reprompt,
            )
            _spec_validation = _validate_spec(
                loaded_spec, repo_path, prep.base_sha,
            )
            append_event(spec.run_id, "spec_validation",
                         all_satisfied=_spec_validation.all_satisfied,
                         total=len(_spec_validation.results),
                         unsatisfied_ids=[
                             r.requirement.id
                             for r in _spec_validation.unsatisfied
                         ])

        # v1.3 directive reprompt path — fires when no spec OR spec is fully
        # satisfied (in which case the gate below short-circuits to no-op
        # before computing diff_text).
        _reprompt_diff = (
            _diff_against_base(repo_path, prep.base_sha)
            if detected_task in _WRITE_TASKS else None
        )
        # Gate selection:
        #   - If a spec is loaded AND has unsatisfied requirements, use the
        #     SpecDD structured reprompt.
        #   - Else, fall through to v1.3 diff-size heuristic.
        _spec_reprompt_fires = (
            _spec_validation is not None
            and not _spec_validation.all_satisfied
        )
        _v1_3_reprompt_fires = (
            _spec_validation is None
            and _reprompt_diff is not None
            and _should_reprompt_for_under_engagement(
                detected_task, _reprompt_diff[0]))

        if _spec_reprompt_fires or _v1_3_reprompt_fires:
            additions, deletions, diff_text = (
                _reprompt_diff if _reprompt_diff is not None else (0, 0, "")
            )
            if _spec_reprompt_fires:
                # SpecDD path — structured per-requirement reprompt. The
                # diff state is informational; the gate is which requirements
                # are unmet.
                console.print(
                    f"\n[bold cyan]▶ Reprompt 2nd pass[/]  "
                    f"(spec: {len(_spec_validation.unsatisfied)}/"
                    f"{len(_spec_validation.results)} requirement(s) unmet)"
                )
                append_event(spec.run_id, "reprompt_fired",
                             additions=additions, deletions=deletions,
                             threshold=_REPROMPT_DOC_ADDITIONS_THRESHOLD,
                             gate="spec")
                followup_goal = (
                    f"You completed an initial pass on this goal:\n  {goal}\n\n"
                    + format_unsatisfied_for_reprompt(_spec_validation)
                )
            else:
                # v1.3 directive path — preserved verbatim for fixtures
                # without a spec. Branches on the prose-mode signature
                # (additions==0 AND substantial prior prose).
                console.print(
                    f"\n[bold cyan]▶ Reprompt 2nd pass[/]  "
                    f"(diff +{additions}/-{deletions} below threshold "
                    f"{_REPROMPT_DOC_ADDITIONS_THRESHOLD})"
                )
                append_event(spec.run_id, "reprompt_fired",
                             additions=additions, deletions=deletions,
                             threshold=_REPROMPT_DOC_ADDITIONS_THRESHOLD,
                             gate="v1_3_directive")
                prior_text = single_result.final_text or ""
                if additions == 0 and len(prior_text) > 1000:
                    followup_goal = (
                        f"PROBLEM: You completed a pass on this goal but did NOT "
                        f"call write_file or edit_file. The working tree has 0 "
                        f"added lines. You produced extensive prose in your "
                        f"final report but it is stranded — not saved to disk.\n\n"
                        f"Original goal:\n  {goal}\n\n"
                        f"Your prior final report (which you must now persist "
                        f"to disk):\n\n{prior_text[:6000]}\n\n"
                        f"Action: identify the file path the goal asks for "
                        f"(e.g., 'CONFIG.md' for an env-var documentation task; "
                        f"the path is named in the goal). Call write_file with "
                        f"that path and a coherent document body derived from "
                        f"the report above. Do this on your FIRST tool call. "
                        f"Do not explore more files first. After write_file "
                        f"succeeds, you may continue if the content needs "
                        f"refinement."
                    )
                else:
                    followup_goal = (
                        f"You completed an initial pass on this goal:\n  {goal}\n\n"
                        f"The diff so far is small ({additions} added / "
                        f"{deletions} deleted lines):\n"
                        f"```diff\n{diff_text}\n```\n\n"
                        f"Re-read the goal carefully. Identify each named deliverable. "
                        f"For any deliverable NOT yet reflected in the diff, make the "
                        f"missing edits now via edit_file or write_file. If you "
                        f"believe the diff is complete, make no further edits and "
                        f"explain in your response which lines satisfy each "
                        f"deliverable."
                    )
            second_result = run_single(
                backend, cfg.role("monolith"),
                goal=followup_goal,
                task_type=detected_task,
                languages=languages,
                extra_tool_defs=extra_tool_defs or None,
                extra_tool_fns=extra_tool_fns or None,
                run_id=spec.run_id,
                phase="reprompt",
            )
            single_result.tool_calls_total += second_result.tool_calls_total
            single_result.schema_rejects += second_result.schema_rejects
            single_result.prompt_tokens += second_result.prompt_tokens
            single_result.completion_tokens += second_result.completion_tokens
            single_result.wall_s += second_result.wall_s
            single_result.tool_calls.extend(second_result.tool_calls)
            if second_result.aborted:
                single_result.aborted = True
                single_result.abort_reason = (
                    "reprompt: " + (second_result.abort_reason or "")
                )
            if second_result.final_text:
                single_result.final_text = (
                    (single_result.final_text or "")
                    + "\n\n--- Reprompt 2nd pass ---\n"
                    + second_result.final_text
                )
                final_report = single_result.final_text
                (run_dir(spec.run_id) / "synthesizer.md").write_text(final_report)
            append_event(spec.run_id, "reprompt_done",
                         second_pass_tool_calls=second_result.tool_calls_total,
                         second_pass_completion_tokens=second_result.completion_tokens,
                         second_pass_aborted=second_result.aborted)
            _ds = _diff_against_base(repo_path, prep.base_sha)
            append_event(spec.run_id, "diff_stat",
                         checkpoint="after_reprompt_pass",
                         additions=_ds[0], deletions=_ds[1])

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
        search_mod.reset_index()
        symbols_mod.reset_index()
        if not keep_loaded:
            try:
                from luxe.backend import Backend as _UnloadBackend
                _ub = _UnloadBackend(model="(unload-probe)")
                results = _ub.unload_all_loaded()
                if results:
                    n_ok = sum(1 for v in results.values() if v)
                    console.print(
                        f"[dim]· Unloaded {n_ok}/{len(results)} model(s) "
                        f"from oMLX (use --keep-loaded to skip)[/]"
                    )
            except Exception as e:
                console.print(f"[dim]· Model unload skipped: {e}[/]")
        try:
            ctx.__exit__(None, None, None)
        except Exception:
            pass


@main.command(name="chat")
@click.option("--repo", "repo", default=".", help="Repo to work in (default: cwd)")
@click.option("--config", "config_path", default=None,
              help="Path to config YAML (default: configs/chat.yaml)")
@click.option("--resume", "resume_session_id", default=None,
              help="Resume a prior chat session by id")
@click.option("--chat-model", default=None, help="Override the chat-slot model")
@click.option("--plan-model", default=None, help="Override the plan-slot model")
@click.option("--code-model", default=None, help="Override the code-slot model")
@click.option("--keep-loaded", is_flag=True, default=False,
              help="Skip the post-session model unload.")
@click.option("--dev", "dev_mode", is_flag=True, default=False,
              help="Start in dev mode: write tools + unrestricted shell ON "
                   "(equivalent to /write + /bash). Skips per-session toggling.")
@click.option("--verbose", "startup_verbose", default=None,
              type=click.Choice(["off", "diff", "full"]),
              help="Set tool-output verbosity at startup (e.g. before a /goal run).")
@click.option("--show-reasoning", "startup_show_reasoning", is_flag=True, default=False,
              help="Stream the model's reasoning live from startup.")
@click.option("--no-terse", "startup_no_terse", is_flag=True, default=False,
              help="Disable terse model output (terse is ON by default).")
@click.option("--debug", "startup_debug", is_flag=True, default=False,
              help="Show everything: verbose full + reasoning (overrides --verbose).")
@click.option("--compact", "startup_compact", is_flag=True, default=False,
              help="Compact display: tighter on-screen output ceiling.")
@click.option("--theme", "theme_name", default=None,
              help="Curated luxe color palette: auto|cool|warm|mono (default: auto).")
def chat_cmd(
    repo: str, config_path: str | None, resume_session_id: str | None,
    chat_model: str | None, plan_model: str | None, code_model: str | None,
    keep_loaded: bool, dev_mode: bool,
    startup_verbose: str | None, startup_show_reasoning: bool,
    startup_no_terse: bool, startup_debug: bool, startup_compact: bool,
    theme_name: str | None,
):
    """Interactive terminal agent (Claude-CLI-style). Default: champion in
    every slot, read-only tools (toggle with /write)."""
    from luxe.chat import run_chat_repl
    from luxe.locks import LockHeld, acquire_repo_lock
    from luxe.tools.fs import set_repo_root

    # Theme precedence (D2): --theme flag → LUXE_THEME env → shipped default 'cool'.
    # The curated palette thus applies without a flag; `--theme auto` (or
    # LUXE_THEME=auto) restores terminal/YASL tracking.
    theme_name = theme_name or os.environ.get("LUXE_THEME") or "cool"

    repo_path = _resolve_repo(repo)
    cfg = load_config(config_path or _default_chat_config())

    # CLI per-slot overrides become an ad-hoc model + slots block so the user
    # can point a slot at any oMLX-loadable model without editing YAML.
    _apply_slot_overrides(cfg, chat_model, plan_model, code_model)

    set_repo_root(repo_path)

    from luxe import search as search_mod
    from luxe import symbols as symbols_mod
    console.print("[dim]· Indexing repository for search (model loads on first turn)…[/]")
    bm25 = search_mod.build_bm25_index(repo_path)
    sym_idx = symbols_mod.build_symbol_index(repo_path)
    search_mod.set_index(bm25)
    symbols_mod.set_index(sym_idx)
    console.print(f"[dim]  scanned {len(bm25.paths)} files · "
                  f"{len(sym_idx.symbols)} symbols[/]")
    languages = _detect_languages_for_repo(repo_path)

    try:
        ctx = acquire_repo_lock(repo_path, f"chat-{int(time.time())}")
        ctx.__enter__()
    except LockHeld as e:
        console.print(f"[red]✗ {e}[/]")
        sys.exit(3)

    # Front-end selection: the full-screen Textual TUI when stdout is a real
    # terminal AND textual is installed AND not resuming (resume lives in the line
    # REPL); otherwise the line REPL (CI / pipes / textual-absent). chat.sdd.
    run_app = None
    if sys.stdout.isatty() and not resume_session_id:
        try:
            from luxe.chat.tui import run_chat_app as run_app
        except Exception:
            run_app = None
            console.print("[dim]· textual not installed — using the line REPL "
                          "(pip install 'luxe[chat]' for the full-screen UI)[/]")

    try:
        if run_app is not None:
            run_app(
                cfg, repo_path, languages,
                keep_loaded=keep_loaded,
                dev_mode=dev_mode,
                startup_verbose=startup_verbose,
                startup_show_reasoning=startup_show_reasoning,
                startup_no_terse=startup_no_terse,
                startup_debug=startup_debug,
                startup_compact=startup_compact,
                theme_name=theme_name,
            )
        else:
            run_chat_repl(
                cfg, repo_path, languages,
                console=console,
                keep_loaded=keep_loaded,
                resume_session_id=resume_session_id,
                dev_mode=dev_mode,
                startup_verbose=startup_verbose,
                startup_show_reasoning=startup_show_reasoning,
                startup_no_terse=startup_no_terse,
                startup_debug=startup_debug,
                startup_compact=startup_compact,
                theme_name=theme_name,
            )
    finally:
        search_mod.reset_index()
        symbols_mod.reset_index()
        try:
            ctx.__exit__(None, None, None)
        except Exception:
            pass


def _default_chat_config() -> str:
    return str(Path(__file__).parent.parent.parent / "configs" / "chat.yaml")


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
    cfg = load_config(config_path or _default_chat_config())
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
        if not keep_loaded:
            from luxe.backend import Backend
            try:
                Backend(model="(unload-probe)").unload_all_loaded()
            except Exception:
                pass


@compare_group.command(name="review")
@click.argument("compare_id", required=False, default="")
def compare_review_cmd(compare_id):
    """Replay a stored comparison and tally its votes (no arg: list them)."""
    from luxe.compare import store
    store.review(compare_id, console=console)


def _run_gitkit_cmd(kind: str, repo: str, config_path: str | None,
                    keep_loaded: bool, save: bool, verbose: bool = False,
                    deep: bool | None = None, max_chunks: int | None = None,
                    rebuild_map: bool = False, mirror: bool = True) -> None:
    """Shared body for the gitsummary/gitreview/gitrefactor CLI commands. The
    runner owns target resolution (incl. cloning a URL when the path is not a
    git repo), index building, and repo_root; here we only clone an explicit URL
    arg up front, load the config, and unload models afterward.

    `deep` (None=auto by footprint, True/False force), `max_chunks`, and
    `rebuild_map` pass straight through to the runner's deep-mode dispatch."""
    from luxe.gitkit import run_git_report

    # An explicit URL arg clones immediately (no prompt). A local path is passed
    # through; the runner prompts to clone if it isn't a git working tree.
    if repo.startswith(("http://", "https://", "git@", "ssh://")):
        repo_path = _resolve_repo(repo, full_history=(kind == "gitsummary"))
    else:
        repo_path = str(Path(repo).expanduser().resolve())
    cfg = load_config(config_path or _default_chat_config())

    try:
        run_git_report(kind, cfg=cfg, repo_path=repo_path,
                       console=console, save=save, verbose=verbose,
                       deep=deep, max_chunks=max_chunks, rebuild_map=rebuild_map,
                       mirror=mirror)
    finally:
        if not keep_loaded:
            from luxe.backend import Backend
            try:
                Backend(model="(unload-probe)").unload_all_loaded()
            except Exception:
                pass


def _gitkit_options(f):
    """Shared options for the three gitkit commands (incl. deep-mode flags)."""
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
    f = click.option("--no-mirror", is_flag=True, default=False,
                     help="Don't write the committable <repo>/.luxe/gitkit/ mirror")(f)
    return f


@main.command(name="gitsummary")
@_gitkit_options
def gitsummary_cmd(repo, config_path, keep_loaded, no_save, verbose,
                   deep, max_chunks, rebuild_map, no_mirror):
    """Summarize a repo: purpose, deps, health, and a use-risk verdict."""
    _run_gitkit_cmd("gitsummary", repo, config_path, keep_loaded, not no_save,
                    verbose, deep=deep, max_chunks=max_chunks,
                    rebuild_map=rebuild_map, mirror=not no_mirror)


@main.command(name="gitreview")
@_gitkit_options
def gitreview_cmd(repo, config_path, keep_loaded, no_save, verbose,
                  deep, max_chunks, rebuild_map, no_mirror):
    """Review a repo for serious bugs and security concerns (read-only)."""
    _run_gitkit_cmd("gitreview", repo, config_path, keep_loaded, not no_save,
                    verbose, deep=deep, max_chunks=max_chunks,
                    rebuild_map=rebuild_map, mirror=not no_mirror)


@main.command(name="gitrefactor")
@_gitkit_options
def gitrefactor_cmd(repo, config_path, keep_loaded, no_save, verbose,
                    deep, max_chunks, rebuild_map, no_mirror):
    """Propose an ordered structural refactor plan for a repo (read-only)."""
    _run_gitkit_cmd("gitrefactor", repo, config_path, keep_loaded, not no_save,
                    verbose, deep=deep, max_chunks=max_chunks,
                    rebuild_map=rebuild_map, mirror=not no_mirror)


apply_aliases(main, {
    "git-summary": "gitsummary", "gsum": "gitsummary",
    "git-review": "gitreview", "grev": "gitreview",
    "git-refactor": "gitrefactor", "gref": "gitrefactor",
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


def _default_config() -> str:
    return str(Path(__file__).parent.parent.parent / "configs" / "single_64gb.yaml")


def _infer_task_type(goal: str) -> str:
    g = goal.lower()
    if any(k in g for k in (
        "implement", "add ", "build", "create", "introduce", "refactor", "rewrite",
        "optimize", "change", "modify", "delete", "remove", "support", "improve",
        "tweak", "adjust", "polish", "re-implement", "update", "migrate", "port",
        "enable", "disable", "clean", "restructure"
    )):
        return "implement"
    if any(k in g for k in (
        "fix", "bug", "broken", "regression", "patch", "resolve", "correct",
        "mend", "handle"
    )):
        return "bugfix"
    if any(k in g for k in (
        "document", "docs", "readme", "docstring", "comment", "documentation",
        "typehint", "typing", "types"
    )):
        return "document"
    if any(k in g for k in (
        "update deps", "upgrade", "ci", "config", "dep", "dependency", "docker",
        "github action", "workflow"
    )):
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


if __name__ == "__main__":
    main()
