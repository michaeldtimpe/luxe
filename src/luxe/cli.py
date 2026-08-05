"""CLI entry point for luxe — mono-only execution."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

import click
from rich.console import Console

from luxe import gitclone
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
        ok, err = gitclone.clone(repo, clone_dir, full_history=full_history)
        if not ok:
            console.print(f"[red]Clone failed:[/] {err}")
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


from luxe.pr import diff_against_base as _diff_against_base  # moved to pr.py (shared)


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
        console.print("Tests: [dim](none detected)[/]")

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
                    console.print("\n[yellow]· No PR opened (no diff produced)[/]")
            except pr_mod.NoMutationsError as e:
                console.print(f"\n[red]✗ {e}[/]")
                console.print("[dim]Status: failed_no_mutations_produced. "
                              "Resume not applicable.[/]")
                sys.exit(4)
            except pr_mod.PRError as e:
                console.print(f"\n[red]✗ PR cycle blocked: {e}[/]")
                console.print(f"[dim]Resume with: luxe pr {spec.run_id}[/]")
                sys.exit(5)
        elif detected_task in {"review", "summarize"}:
            console.print("\n[dim](read-only task; no PR)[/]")

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


# `luxe chat` / `luxe code` — thin Click shells. The shared option list, the
# posture wiring, and the whole startup path live in chat/launch.py. The
# non-shell names are re-exported so `luxe.cli._X` keeps resolving for the
# tests and scripts that import them there.
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


def _default_chat_config() -> str:
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
    cfg = load_config(config_path or _default_chat_config())

    try:
        run_git_report(kind, cfg=cfg, repo_path=repo_path,
                       console=console, save=save, verbose=verbose,
                       deep=deep, max_chunks=max_chunks, rebuild_map=rebuild_map,
                       mirror=mirror, base=base, pr=pr,
                       min_severity=min_severity, no_incremental=no_incremental)
    finally:
        if not keep_loaded:
            from luxe.backend import Backend
            try:
                Backend(model="(unload-probe)").unload_all_loaded()
            except Exception:
                pass


def _run_gitapply_cmd(repo: str, config_path: str | None, keep_loaded: bool,
                      *, deep: bool | None = None, rebuild_map: bool = False) -> None:
    """Body for `gitchange --apply` / `gitapply`: execute a saved plan against a local
    repo. Apply NEVER clones — it only runs on a real checkout the user controls."""
    from luxe.gitkit import apply as apply_mod

    if repo.startswith(("http://", "https://", "git@", "ssh://")):
        console.print("[red]gitapply does not clone — point it at a local repo path.[/]")
        raise SystemExit(2)
    repo_path = str(Path(repo).expanduser().resolve())
    cfg = load_config(config_path or _default_chat_config())
    try:
        rc = apply_mod.run_apply(repo_path=repo_path, cfg=cfg, console=console,
                                 deep=deep, rebuild_map=rebuild_map)
    finally:
        if not keep_loaded:
            from luxe.backend import Backend
            try:
                Backend(model="(unload-probe)").unload_all_loaded()
            except Exception:
                pass
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

    if remove_state:
        _pull_remove(ref, dest_dir, force=force, assume_yes=assume_yes)
        return

    with ms.OmlxAdmin(base_url=endpoint) as admin:
        try:
            if search_query:
                _pull_search(admin, search_query)
                return
            if list_state:
                _pull_list(admin, dest_dir,
                           endpoint=endpoint, base_url_given=bool(base_url))
                return
            if not ref:
                console.print("[yellow]Nothing to do — pass a model "
                              "(`luxe pull mlx-community/Qwen3.6-27B-6bit`), "
                              "`--search <query>`, or `--list`.[/]")
                sys.exit(2)

            name = ms.store_name_for(ref)
            if from_path:
                src = ms._resolve_hf_snapshot(Path(from_path).expanduser())
                if src is None:
                    console.print(f"[red]✗ {from_path} is not an MLX model "
                                  "directory (needs config.json + weights).[/]")
                    sys.exit(2)
                sources = [ms.ModelSource(kind="mount", ref=str(src), name=name,
                                          size_bytes=ms.dir_size(src),
                                          note="--from")]
            else:
                if not force_hf:
                    console.print("[dim]· scanning mounted volumes…[/]")
                sources = ms.resolve_sources(ref, admin=admin,
                                             include_mounts=not force_hf)
            if not sources:
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
    `uv sync --extra chat`. Says what's incoming before touching anything;
    no-op (and says so) when already current. Targets the luxe source repo
    regardless of where you run it."""
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
        return sp.run(["git", "-C", str(root), *args],
                      capture_output=True, text=True, timeout=timeout)

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
        # lint/typecheck/security tools shell-outs. A bare `--extra chat`
        # sync pruned dev from the m1 and broke the drill (2026-07-30).
        extras = ["--extra", "chat", "--extra", "dev", "--extra", "analyzers"]
        with console.status("[dim]uv sync (chat+dev+analyzers)…[/]"):
            try:
                synced = sp.run(["uv", "sync", *extras],
                                cwd=str(root), capture_output=True, text=True,
                                timeout=600)
            except FileNotFoundError:
                synced = None
        if synced is None:
            console.print("[yellow]⚠ uv not on PATH — run "
                          "`uv sync --extra chat --extra dev --extra "
                          "analyzers` in the repo yourself[/]")
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

    cfg = load_config(config_path or _default_chat_config())
    if backend_name and backend_name not in cfg.backend_entries():
        console.print(f"[red]✗ Unknown backend {backend_name!r}. "
                      f"Configured: {', '.join(cfg.backend_entries())}.[/]")
        sys.exit(2)
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
    from luxe.chat import project as project_mod
    from luxe.chat.session import ChatSession
    from luxe.chat.slots import SlotManager

    project = project_mod.resolve(repo_path)
    session = ChatSession(repo_path=project.root, project_kind=project.kind)
    doc = inspection.run_doctor(session, SlotManager(cfg), project.root)
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
    cfg = load_config(config_path or _default_chat_config())
    if backend_name:
        entries = cfg.backend_entries()
        if backend_name not in entries:
            console.print(f"[red]✗ Unknown backend {backend_name!r}. "
                          f"Configured: {', '.join(entries)}.[/]")
            sys.exit(2)
        cfg.backends = {k: v.model_copy(update={"default": k == backend_name})
                        for k, v in entries.items()}

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

    cfg = load_config(config_path or _default_chat_config())
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
        cfg = load_config(config_path or _default_chat_config())
    except Exception:
        cfg = None
    anchor = host or netdiag.ANCHOR_HOST

    def _render(report) -> None:
        for ok, line in netdiag.render_lines(report):
            glyph = "[green]✓[/]" if ok else "[red]✗[/]"
            console.print(f"  {glyph} {line}")
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
    log_path = Path.home() / ".luxe" / "netwatch.log"
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
        for ok, line in pp.render_lines(report):
            glyph = "[green]✓[/]" if ok else "[red]✗[/]"
            console.print(f"  {glyph} {line}")
    sys.exit(0 if report.verdict == pp.PP_OK else 1)


def _omlx_base_url_from_config() -> str:
    """The chat config's oMLX endpoint, falling back to the local default."""
    try:
        return load_config(_default_chat_config()).omlx_base_url
    except Exception:
        return "http://127.0.0.1:8000"


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
        manifest = load_config(_default_chat_config()).host_manifest()
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
               base_url_given: bool = False) -> None:
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


_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".jsx": "javascript", ".rs": "rust",
    ".go": "go",
}


def _languages_from_paths(paths) -> frozenset[str]:
    """Languages present in an already-enumerated file list — no walk.

    `luxe chat` calls this with the scan it built for the indexes; walking the
    tree a third time cost ~18s from `$HOME` (measured 2026-07-30), and with
    weaker pruning than the indexes used.
    """
    return frozenset(
        lang for p in paths
        if (lang := _LANG_BY_EXT.get(Path(p).suffix.lower())) is not None
    )


def _detect_languages_for_repo(repo_path: str) -> frozenset[str]:
    """Walk `repo_path` to find which languages it contains.

    Still the walking version for `maintain`/gitkit (unchanged behavior). The
    chat path uses `_languages_from_paths` instead — see above.
    """
    found: set[str] = set()
    import os as _os
    for root, dirs, files in _os.walk(Path(repo_path)):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"}]
        for f in files:
            ext = Path(f).suffix.lower()
            if ext in _LANG_BY_EXT:
                found.add(_LANG_BY_EXT[ext])
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
