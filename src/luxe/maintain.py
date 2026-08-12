"""The `luxe maintain` pipeline: preflight → index → agent → lint → PR.

Lifted verbatim out of `cli.py` 2026-08-04. `cli.maintain` is now a thin Click
shell over `maintain_pipeline`; the two benchmark adapters still spawn
`python -m luxe.cli maintain`, so the command, its arguments, and its options
are unchanged.

This is the DETERMINISTIC path (luxe.sdd): no streaming, no chat seams, no
`on_token`. Repo resolution and task-type inference come from their tier
homes (`luxe.gitclone.resolve_repo`, `luxe.agents.tasktype.infer_task_type`
— moved out of `cli` 2026-08-05, deferred-list #6), so this module no
longer imports `cli` at all.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

import click
from rich.console import Console

from luxe.agents.tasktype import infer_task_type
from luxe.config import load_config
from luxe.gitclone import resolve_repo
from luxe.pr import GitDiffError, diff_against_base as _diff_against_base
from luxe.repo_index import _detect_languages_for_repo

console = Console()


def _default_config() -> str:
    return str(Path(__file__).parent.parent.parent / "configs" / "single_64gb.yaml")


_WRITE_TASKS = {"implement", "bugfix", "document", "manage"}


# v1.3 probe: re-prompt-on-under-engagement lever for doc tasks. The B1+B2
# overlay attempts (v1.1 abstract / v1.2 procedural anchor) both failed to
# unblock lpe-typing's under-engagement at the model scale. This is a
# runtime lever instead: after the agent loop finishes, if a doc-task diff
# is suspiciously small, re-invoke the agent with the goal + actual diff
# and a directive to find missing deliverables. Hardcoded threshold for
# the probe; if the lever lands, promote to RoleConfig.
_REPROMPT_DOC_ADDITIONS_THRESHOLD = 10


def apply_ctx_read_budget(num_ctx: int) -> int | None:
    """Wire `LUXE_TOOL_BUDGET_CTX` into the benchmark/maintain path.

    The ctx-derived tool-output budget (`tools/fs.py`, 2026-08-12) had exactly
    one caller — the chat REPL — so setting the env var in front of a
    maintain_suite run was INERT BY CONSTRUCTION: the flag never reached the
    path it was supposed to change. This is that reach.

    **Default ON here since 2026-08-12** (`acceptance/toolbudget_ab_2026_08_12/
    REPORT.md`: 3 reps × 10 fixtures × 2 arms, both arms 30/30 · 120/150,
    tokens −3.9%, wall +4.3%, zero fixture regressions, opportunity exercised
    — 21 baseline over-budget reads vs zero in treatment). The opt-out grammar
    is the one `LUXE_TRUNCATED_TURN_RETRY` uses: only the exact string "0"
    disables. Unset → ON; "0" → OFF; any other value ("1", "", "true") → ON.

    Returns the applied budget in bytes, or `None` when the switch is off — in
    which case `set_read_budget` is NOT called at all, so the fixed 256 KB
    constants stand and the run is byte-identical to one from before this
    function existed. Chat keeps its own wiring and stays OPT-IN (`=1`) — no
    chat-side evidence exists; `chat/repl.py` sets the budget per turn, because
    `/ctx` moves num_ctx mid-session. This function is reached only from
    `maintain_pipeline`, which chat never enters.
    """
    if os.environ.get("LUXE_TOOL_BUDGET_CTX", "1") == "0":
        return None
    from luxe.tools.fs import budget_for_ctx, set_read_budget
    budget = budget_for_ctx(num_ctx)
    set_read_budget(budget)
    return budget


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


def maintain_pipeline(
    repo: str, goal: str, task_type: str | None,
    config_path: str | None,
    allow_dirty: bool, skip_confirm: bool, watch_ci: bool,
    output_dir: str, save_report: bool, keep_loaded: bool,
    spec_yaml_path: str | None,
) -> None:
    """Body of `luxe maintain` — see `cli.maintain` for the option surface."""
    from luxe.agents.single import run_single
    from luxe.backend import Backend
    from luxe.citations import lint_report
    from luxe.locks import LockHeld, acquire_repo_lock
    from luxe import pr as pr_mod
    from luxe.run_state import RunSpec, append_event, init_run_dir, run_dir
    from luxe.tools.fs import set_repo_root

    repo_path = resolve_repo(repo)
    detected_task = task_type or infer_task_type(goal)

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

        # Ctx-derived tool-output budget — DEFAULT ON here since 2026-08-12
        # (benched: acceptance/toolbudget_ab_2026_08_12/REPORT.md). Opt out
        # with the exact string `LUXE_TOOL_BUDGET_CTX=0`, which restores the
        # fixed 256 KB constants; off = no call at all, byte-identical run.
        # On = the budget for the SAME role num_ctx this run executes with.
        # Announced loudly so a bench run can be checked for liveness
        # (tools.sdd). The announce line is pinned to the treatment arm's
        # wording so the shipped default is byte-equivalent to what was
        # measured with the flag set to "1".
        _read_budget = apply_ctx_read_budget(cfg.role("monolith").num_ctx)
        if _read_budget is not None:
            console.print(
                f"[yellow]· Tool read budget: {_read_budget:,} bytes[/] "
                f"[dim](LUXE_TOOL_BUDGET_CTX=1, num_ctx="
                f"{cfg.role('monolith').num_ctx})[/]"
            )
            append_event(spec.run_id, "read_budget_applied",
                         max_bytes=_read_budget,
                         num_ctx=cfg.role("monolith").num_ctx)

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
            # A failed `git diff` is recorded as a failure, never as +0/-0 —
            # the telemetry would otherwise read as "the agent wrote nothing".
            try:
                _ds = _diff_against_base(repo_path, prep.base_sha)
            except GitDiffError as e:
                append_event(spec.run_id, "diff_stat_failed",
                             checkpoint="after_main_pass", error=str(e))
            else:
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
                         error=_spec_validation.error,
                         unsatisfied_ids=[
                             r.requirement.id
                             for r in _spec_validation.unsatisfied
                         ])
            if _spec_validation.error:
                # The diff could not be read, so the unmet verdicts are about
                # git, not about the agent. Reprompting on them would hand the
                # model a fabricated "requirement unsatisfied" list.
                console.print(f"[yellow]· spec validation ERROR: "
                              f"{_spec_validation.error} — reprompt gate "
                              f"skipped[/]")

        # v1.3 directive reprompt path — fires when no spec OR spec is fully
        # satisfied (in which case the gate below short-circuits to no-op
        # before computing diff_text).
        # `None` = "no diff to gate on": for a read-only task there never was
        # one, and after a GitDiffError there is no honest number to gate on
        # either — the under-engagement heuristic below must not read a git
        # failure as 0 added lines and fire a second pass on it.
        _reprompt_diff = None
        if detected_task in _WRITE_TASKS:
            try:
                _reprompt_diff = _diff_against_base(repo_path, prep.base_sha)
            except GitDiffError as e:
                append_event(spec.run_id, "diff_stat_failed",
                             checkpoint="reprompt_gate", error=str(e))
                console.print(f"[yellow]· diff unavailable ({e}) — "
                              f"under-engagement gate skipped[/]")
        # Gate selection:
        #   - If a spec is loaded AND has unsatisfied requirements, use the
        #     SpecDD structured reprompt.
        #   - Else, fall through to v1.3 diff-size heuristic.
        _spec_reprompt_fires = (
            _spec_validation is not None
            and _spec_validation.error is None
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
            try:
                _ds = _diff_against_base(repo_path, prep.base_sha)
            except GitDiffError as e:
                append_event(spec.run_id, "diff_stat_failed",
                             checkpoint="after_reprompt_pass", error=str(e))
            else:
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
