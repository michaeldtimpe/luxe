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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import click
from rich.console import Console

from luxe import pr as pr_mod
from luxe import run_state
from luxe.agents.prompts import (
    REPROMPT_SMALL_DIFF_TEMPLATE,
    REPROMPT_SPEC_TEMPLATE,
    REPROMPT_STRANDED_PROSE_TEMPLATE,
)
from luxe.agents.tasktype import infer_task_type
from luxe.config import load_config
from luxe.gitclone import resolve_repo
from luxe.pr import GitDiffError, diff_against_base as _diff_against_base
from luxe.repo_index import _detect_languages_for_repo

if TYPE_CHECKING:
    from luxe.agents.loop import AgentResult
    from luxe.backend import Backend
    from luxe.citations import LintResult
    from luxe.config import PipelineConfig, RoleConfig
    from luxe.pr import PRConfig, PRPreflight
    from luxe.run_state import RunSpec
    from luxe.spec import Spec
    from luxe.spec_validator import ValidationResult

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


def _v1_3_followup_goal(goal: str, additions: int, deletions: int,
                        diff_text: str, prior_text: str) -> str:
    """The v1.3 directive reprompt's goal — for fixtures without a spec.
    Branches on the prose-mode signature (additions==0 AND substantial
    prior prose). Wording lives in agents/prompts.py."""
    if additions == 0 and len(prior_text) > 1000:
        return REPROMPT_STRANDED_PROSE_TEMPLATE.format(
            goal=goal, prior_text=prior_text[:6000])
    return REPROMPT_SMALL_DIFF_TEMPLATE.format(
        goal=goal, additions=additions, deletions=deletions,
        diff_text=diff_text)


def _merge_pass(into, other) -> None:
    """Fold a second pass's (reprompt / step-limit finish) counters and
    tool calls into the main pass's AgentResult."""
    into.tool_calls_total += other.tool_calls_total
    into.schema_rejects += other.schema_rejects
    into.prompt_tokens += other.prompt_tokens
    into.completion_tokens += other.completion_tokens
    into.wall_s += other.wall_s
    into.tool_calls.extend(other.tool_calls)


@dataclass
class _Pipeline:
    """One `maintain_pipeline` run's shared state, built once the repo lock is
    held and handed to every step helper below (the shape `_Run` gives
    `run_agent`). `cfg`, `backend` and `languages` are filled by `_load_model`
    inside the guarded block. `report` is the text that reaches
    synthesizer.md, the PR, the saved report and the console; the main pass
    sets it, and the reprompt and step-limit label rewrite it."""

    repo_path: str
    goal: str
    task: str
    spec: RunSpec
    prep: PRPreflight
    extra_tool_defs: list
    extra_tool_fns: dict
    cfg: PipelineConfig = field(init=False)
    backend: Backend = field(init=False)
    languages: frozenset[str] = field(init=False)
    report: str = ""

    @property
    def writes(self) -> bool:
        return self.task in _WRITE_TASKS

    def emit(self, kind: str, **data: Any) -> None:
        run_state.append_event(self.spec.run_id, kind, **data)

    def save_synthesizer(self) -> None:
        (run_state.run_dir(self.spec.run_id) / "synthesizer.md").write_text(
            self.report)


def maintain_pipeline(
    repo: str, goal: str, task_type: str | None,
    config_path: str | None,
    allow_dirty: bool, skip_confirm: bool, watch_ci: bool,
    output_dir: str, save_report: bool, keep_loaded: bool,
    spec_yaml_path: str | None,
) -> None:
    """Body of `luxe maintain` — see `cli.maintain` for the option surface.

    Preflight, the run dir, the repo lock, the indices and MCP come first and
    exit on failure (codes 2 and 3). The rest runs under `_teardown`: model
    load, main pass, citation lint, the opt-in reprompt, step-limit handling,
    the cleanup turn, the PR cycle (codes 4 and 5) and the report."""
    # Loaded here, before preflight, as they were before the step helpers
    # (which import from them) were split out of this function.
    import luxe.agents.single  # noqa: F401
    import luxe.backend  # noqa: F401
    import luxe.citations  # noqa: F401
    import luxe.locks  # noqa: F401
    import luxe.tools.fs  # noqa: F401

    repo_path = resolve_repo(repo)
    detected_task = task_type or infer_task_type(goal)
    loaded_spec = _load_spec(spec_yaml_path)
    confirm_callback = _confirm_callback(skip_confirm)
    pr_cfg = pr_mod.load_pr_config()
    prep = _preflight(repo_path, detected_task, goal, allow_dirty,
                      confirm_callback, pr_cfg)

    spec = run_state.RunSpec(
        goal=goal,
        task_type=detected_task,
        repo_path=str(Path(repo_path).resolve()),
        base_sha=prep.base_sha,
        base_branch=prep.base_branch,
    )
    run_state.init_run_dir(spec)
    run_state.append_event(spec.run_id, "preflight_ok",
                           base_branch=prep.base_branch,
                           branch_name=prep.branch_name,
                           test_command=prep.test_command)
    _print_header(spec, repo_path, goal, detected_task, prep)

    lock = _acquire_lock(spec)
    _build_indices(repo_path)
    mcp_mgr, extra_tool_defs, extra_tool_fns = _start_mcp(detected_task)

    try:
        p = _Pipeline(repo_path=repo_path, goal=goal, task=detected_task,
                      spec=spec, prep=prep, extra_tool_defs=extra_tool_defs,
                      extra_tool_fns=extra_tool_fns)
        _load_model(p, config_path)
        result = _main_pass(p)
        _lint(p)
        _reprompt(p, result, loaded_spec)
        step_limited = _step_limit(p, result)
        _cleanup_turn(p)
        _pr_cycle(p, pr_cfg, watch_ci, step_limited)
        _emit_report(p, save_report, output_dir)
    finally:
        _teardown(mcp_mgr, keep_loaded, lock)


# --- before the guarded block -------------------------------------------

def _load_spec(spec_yaml_path: str | None) -> Spec | None:
    """SpecDD Lever 1 (v1.4-prep): load spec from --spec-yaml if provided.
    Failed loads (missing file, malformed YAML, invalid spec) abort the
    run BEFORE the model is loaded so the user sees the error fast.
    When None, the reprompt block falls back to v1.3 directive behavior."""
    if not spec_yaml_path:
        return None
    import yaml as _yaml
    from luxe.spec import spec_from_yaml_dict
    with open(spec_yaml_path) as _f:
        return spec_from_yaml_dict(_yaml.safe_load(_f) or {})


def _confirm_callback(skip_confirm: bool) -> Callable[[], bool] | None:
    """The `--allow-dirty` confirmation handed to preflight: always yes when
    the prompt is skipped, a typed 'yes' on a TTY, and no callback without
    one."""
    if skip_confirm:
        return lambda: True
    if not sys.stdin.isatty():
        return None

    def _confirm() -> bool:
        click.echo(
            "Type 'yes' to continue with --allow-dirty. Your uncommitted "
            "changes WILL be included in the PR diff."
        )
        return click.prompt("→", default="", show_default=False).strip() == "yes"
    return _confirm


def _preflight(repo_path: str, task: str, goal: str, allow_dirty: bool,
               confirm_callback: Callable[[], bool] | None,
               pr_cfg: PRConfig) -> PRPreflight:
    """`pr.preflight`; a missing gh login or a dirty tree exits 2."""
    try:
        return pr_mod.preflight(
            repo_path,
            task_type=task,
            goal=goal,
            allow_dirty=allow_dirty,
            confirm_callback=confirm_callback,
            cfg=pr_cfg,
        )
    except (pr_mod.GhAuthError, pr_mod.DirtyTreeError) as e:
        console.print(f"[red]✗ {e}[/]")
        sys.exit(2)


def _print_header(spec: RunSpec, repo_path: str, goal: str, task: str,
                  prep: PRPreflight) -> None:
    console.print(f"\n[bold]luxe maintain[/]  [dim]run_id={spec.run_id}[/]")
    console.print(f"Repo: {repo_path}")
    console.print(f"Goal: {goal}")
    console.print(f"Task: {task}")
    branch_display = prep.branch_name or "(none)"
    console.print(f"Branch: [dim]{branch_display}[/]  Base: [dim]{prep.base_branch}@{prep.base_sha[:8]}[/]")
    if prep.test_command:
        console.print(f"Tests: [dim]{prep.test_command}[/]")
    else:
        console.print("Tests: [dim](none detected)[/]")


def _acquire_lock(spec: RunSpec) -> Any:
    """Enter the repo lock (released by `_teardown`); another holder exits 3."""
    from luxe.locks import LockHeld, acquire_repo_lock
    try:
        lock = acquire_repo_lock(spec.repo_path, spec.run_id)
        lock.__enter__()
    except LockHeld as e:
        console.print(f"\n[red]✗ {e}[/]")
        sys.exit(3)
    return lock


def _build_indices(repo_path: str) -> None:
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


def _start_mcp(task: str) -> tuple[Any, list, dict]:
    """Start the MCP servers this task opted into; returns the manager (None
    when none did) and the tool defs/fns they contribute."""
    from luxe.mcp.client import MCPClientManager, load_mcp_config
    mcp_cfg = load_mcp_config()
    # OPT-IN per task: a server reaches the maintain/benchmark tool surface
    # only when its `enabled_for` names this task. An empty list used to mean
    # "every task", so registering the chat-oriented codex_one + cpa servers
    # (1925950, 2026-08-25) silently added 14 tools to every bench run since.
    # Servers without an opt-in are not even started here; `luxe chat --mcp`
    # attaches by name and is unaffected.
    mcp_cfg.servers = [s for s in mcp_cfg.servers
                       if task in s.enabled_for]
    mcp_mgr: MCPClientManager | None = None
    extra_tool_defs: list = []
    extra_tool_fns: dict = {}
    if mcp_cfg.servers:
        mcp_mgr = MCPClientManager(mcp_cfg).start()
        extra_tool_defs, extra_tool_fns = mcp_mgr.discover_tools(
            only_for_task=task,
        )
        if extra_tool_defs:
            console.print(f"[dim]· MCP: {len(extra_tool_defs)} tool(s) "
                          f"from {len([s for s in mcp_mgr.server_status() if not s['down']])} "
                          f"server(s)[/]")
        for s in mcp_mgr.server_status():
            if s["down"]:
                console.print(f"[yellow]· MCP server {s['name']} DOWN: "
                              f"{s['down_reason']}[/]")
    return mcp_mgr, extra_tool_defs, extra_tool_fns


# --- the guarded block, in order ----------------------------------------

def _load_model(p: _Pipeline, config_path: str | None) -> None:
    from luxe.backend import Backend
    from luxe.tools.fs import set_repo_root
    cfg = p.cfg = load_config(config_path or _default_config())
    set_repo_root(p.repo_path)
    p.backend = Backend(base_url=cfg.omlx_base_url, model=cfg.model_for_role("monolith"))
    p.languages = _detect_languages_for_repo(p.repo_path)

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
        p.emit("read_budget_applied",
               max_bytes=_read_budget,
               num_ctx=cfg.role("monolith").num_ctx)


def _run_pass(p: _Pipeline, goal: str, phase: str,
              role: RoleConfig | None = None) -> AgentResult:
    """One `run_single` pass with the run's backend, languages and tools;
    `role` replaces the monolith role (the step-limit finish pass)."""
    from luxe.agents.single import run_single
    return run_single(
        p.backend, role if role is not None else p.cfg.role("monolith"),
        goal=goal,
        task_type=p.task,
        languages=p.languages,
        extra_tool_defs=p.extra_tool_defs or None,
        extra_tool_fns=p.extra_tool_fns or None,
        run_id=p.spec.run_id,
        phase=phase,
    )


def _record_diff_stat(p: _Pipeline, checkpoint: str) -> None:
    """A failed `git diff` is recorded as a failure, never as +0/-0 — the
    telemetry would otherwise read as "the agent wrote nothing"."""
    try:
        _ds = _diff_against_base(p.repo_path, p.prep.base_sha)
    except GitDiffError as e:
        p.emit("diff_stat_failed", checkpoint=checkpoint, error=str(e))
    else:
        p.emit("diff_stat", checkpoint=checkpoint,
               additions=_ds[0], deletions=_ds[1])


def _main_pass(p: _Pipeline) -> AgentResult:
    console.print(f"\n[bold cyan]▶ Mono mode[/]  (model: {p.cfg.model_for_role('monolith')})")
    result = _run_pass(p, p.goal, "main")
    p.emit("single_mode_done",
           wall_s=result.wall_s,
           prompt_tokens=result.prompt_tokens,
           completion_tokens=result.completion_tokens,
           tool_calls_total=result.tool_calls_total,
           schema_rejects=result.schema_rejects,
           aborted=result.aborted,
           abort_reason=result.abort_reason,
           final_text_chars=len(result.final_text or ""),
           peak_context_pressure=result.peak_context_pressure)
    if p.writes:
        _record_diff_stat(p, "after_main_pass")
    p.report = result.final_text or ""
    if p.report:
        p.save_synthesizer()
    return result


def _lint(p: _Pipeline) -> None:
    """Citation + spec lint of the report. A blocking result is recorded,
    not enforced: the run goes on to the PR cycle either way."""
    if not p.report:
        return
    from luxe.citations import lint_report
    lint = lint_report(p.report, p.repo_path, base_sha=p.prep.base_sha,
                       envelope=None)
    if lint.is_blocking:
        _lint_blocked(p, lint)
    else:
        console.print(f"\n[green]✓ Lint passed[/] "
                      f"({len(lint.citations)} citations: {lint.summary()})")
        p.emit("citation_lint_passed",
               count=len(lint.citations), summary=lint.summary())
    # Orphans are warning-only at Lever 2 — surface them for human
    # visibility but do not block the run.
    if lint.spec_orphans:
        console.print(
            f"[yellow]· {len(lint.spec_orphans)} spec_orphan warning(s)[/]"
        )
        for so in lint.spec_orphans[:5]:
            console.print(f"    - `{so.path}` (no Owns: glob covers this path)")


def _lint_blocked(p: _Pipeline, lint: LintResult) -> None:
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
    p.emit("citation_lint_blocked",
           unresolved=len(lint.unresolved),
           spec_violations=len(lint.spec_violations),
           summary=lint.summary())


def _reprompt(p: _Pipeline, result: AgentResult, loaded_spec: Spec | None) -> None:
    """The opt-in reprompt 2nd pass (LUXE_REPROMPT_ON_DOC=1). Gate
    selection:
      - If a spec is loaded AND has unsatisfied requirements, use the
        SpecDD structured reprompt.
      - Else, fall through to v1.3 diff-size heuristic."""
    validation = _validate_spec(p, loaded_spec)
    diff = _reprompt_diff(p)
    gate = _reprompt_gate(p, validation, diff)
    if gate is None:
        return
    additions, deletions, diff_text = (
        diff if diff is not None else (0, 0, "")
    )
    if gate == "spec":
        assert validation is not None  # the spec gate fires only on one
        followup_goal = _fire_spec_reprompt(p, validation, additions, deletions)
    else:
        followup_goal = _fire_v1_3_reprompt(p, result, additions, deletions,
                                            diff_text)
    _reprompt_pass(p, result, followup_goal)


def _validate_spec(p: _Pipeline, loaded_spec: Spec | None) -> ValidationResult | None:
    """SpecDD Lever 1 (v1.4-prep): when a spec is provided, the reprompt
    gate uses per-requirement validation. Run validate() once and
    short-circuit the v1.3 path entirely. Still gated by
    LUXE_REPROMPT_ON_DOC=1 so the env-var contract is unchanged."""
    if not (loaded_spec is not None
            and p.writes
            and os.environ.get("LUXE_REPROMPT_ON_DOC") == "1"):
        return None
    from luxe.spec_validator import validate as _validate
    validation = _validate(loaded_spec, p.repo_path, p.prep.base_sha)
    p.emit("spec_validation",
           all_satisfied=validation.all_satisfied,
           total=len(validation.results),
           error=validation.error,
           unsatisfied_ids=[
               r.requirement.id
               for r in validation.unsatisfied
           ])
    if validation.error:
        # The diff could not be read, so the unmet verdicts are about
        # git, not about the agent. Reprompting on them would hand the
        # model a fabricated "requirement unsatisfied" list.
        console.print(f"[yellow]· spec validation ERROR: "
                      f"{validation.error} — reprompt gate "
                      f"skipped[/]")
    return validation


def _reprompt_diff(p: _Pipeline) -> tuple[int, int, str] | None:
    """The diff the v1.3 gate reads. `None` = "no diff to gate on": for a
    read-only task there never was one, and after a GitDiffError there is
    no honest number to gate on either — the under-engagement heuristic
    must not read a git failure as 0 added lines and fire a second pass on
    it."""
    if not p.writes:
        return None
    try:
        return _diff_against_base(p.repo_path, p.prep.base_sha)
    except GitDiffError as e:
        p.emit("diff_stat_failed", checkpoint="reprompt_gate", error=str(e))
        console.print(f"[yellow]· diff unavailable ({e}) — "
                      f"under-engagement gate skipped[/]")
        return None


def _reprompt_gate(p: _Pipeline, validation: ValidationResult | None,
                   diff: tuple[int, int, str] | None) -> str | None:
    """Which reprompt fires: "spec", "v1_3_directive", or None. The v1.3
    path fires only when no spec was validated (a fully satisfied spec
    short-circuits it)."""
    if (validation is not None
            and validation.error is None
            and not validation.all_satisfied):
        return "spec"
    if (validation is None
            and diff is not None
            and _should_reprompt_for_under_engagement(p.task, diff[0])):
        return "v1_3_directive"
    return None


def _fire_spec_reprompt(p: _Pipeline, validation: ValidationResult,
                        additions: int, deletions: int) -> str:
    """SpecDD path — structured per-requirement reprompt. The diff state is
    informational; the gate is which requirements are unmet."""
    from luxe.spec_validator import format_unsatisfied_for_reprompt
    console.print(
        f"\n[bold cyan]▶ Reprompt 2nd pass[/]  "
        f"(spec: {len(validation.unsatisfied)}/"
        f"{len(validation.results)} requirement(s) unmet)"
    )
    p.emit("reprompt_fired",
           additions=additions, deletions=deletions,
           threshold=_REPROMPT_DOC_ADDITIONS_THRESHOLD,
           gate="spec")
    return (
        REPROMPT_SPEC_TEMPLATE.format(goal=p.goal)
        + format_unsatisfied_for_reprompt(validation)
    )


def _fire_v1_3_reprompt(p: _Pipeline, result: AgentResult, additions: int,
                        deletions: int, diff_text: str) -> str:
    """v1.3 directive path — preserved verbatim for fixtures without a
    spec. Branches on the prose-mode signature (additions==0 AND
    substantial prior prose)."""
    console.print(
        f"\n[bold cyan]▶ Reprompt 2nd pass[/]  "
        f"(diff +{additions}/-{deletions} below threshold "
        f"{_REPROMPT_DOC_ADDITIONS_THRESHOLD})"
    )
    p.emit("reprompt_fired",
           additions=additions, deletions=deletions,
           threshold=_REPROMPT_DOC_ADDITIONS_THRESHOLD,
           gate="v1_3_directive")
    return _v1_3_followup_goal(p.goal, additions, deletions, diff_text,
                               result.final_text or "")


def _reprompt_pass(p: _Pipeline, result: AgentResult, followup_goal: str) -> None:
    """Run the 2nd pass and fold it into the main pass's result: counters,
    an abort (prefixed "reprompt: "), and its text appended to the report."""
    second_result = _run_pass(p, followup_goal, "reprompt")
    _merge_pass(result, second_result)
    if second_result.aborted:
        result.aborted = True
        result.abort_reason = (
            "reprompt: " + (second_result.abort_reason or "")
        )
    if second_result.final_text:
        result.final_text = (
            (result.final_text or "")
            + "\n\n--- Reprompt 2nd pass ---\n"
            + second_result.final_text
        )
        p.report = result.final_text
        p.save_synthesizer()
    p.emit("reprompt_done",
           second_pass_tool_calls=second_result.tool_calls_total,
           second_pass_completion_tokens=second_result.completion_tokens,
           second_pass_aborted=second_result.aborted)
    _record_diff_stat(p, "after_reprompt_pass")


def _step_limit(p: _Pipeline, result: AgentResult) -> bool:
    """Step limit with a change on disk (luxe/steplimit.py). The opt-in
    finish pass (LUXE_STEP_LIMIT_FINISH=1) gets one bounded pass to
    complete a half-applied edit; whatever still ends at the limit is
    LABELLED (default ON; LUXE_STEP_LIMIT_LABEL=0 disables): report +
    commit body note, draft PR, `step_limit_incomplete` event. Returns
    whether the run is labelled — the PR cycle's `incomplete`."""
    from luxe import steplimit as _sl
    if not (p.writes and _sl.hit_step_limit(result)):
        return False
    try:
        _sl_diff = _diff_against_base(p.repo_path, p.prep.base_sha)
    except GitDiffError:
        return False
    if not (_sl_diff[0] or _sl_diff[1]):
        return False
    if _sl.finish_enabled() and _finish_pass(p, result, _sl_diff):
        return False
    if not _sl.label_enabled():
        return False
    p.emit("step_limit_incomplete",
           additions=_sl_diff[0], deletions=_sl_diff[1],
           abort_reason=result.abort_reason)
    console.print("[yellow]· stopped at the step limit with a "
                  "change on disk — labelled incomplete "
                  "(draft PR)[/]")
    p.report = _sl.label_report(p.report)
    p.save_synthesizer()
    return True


def _finish_pass(p: _Pipeline, result: AgentResult,
                 diff: tuple[int, int, str]) -> bool:
    """The opt-in step-limit finish pass; True when it ended on its own,
    which clears the label."""
    from luxe import steplimit as _sl
    console.print("\n[bold cyan]▶ Step-limit finish pass[/]")
    p.emit("step_limit_finish_begin",
           additions=diff[0], deletions=diff[1])
    _fin = _run_pass(
        p, _sl.finish_goal(p.goal, diff[2]), "step_limit_finish",
        role=p.cfg.role("monolith").model_copy(
            update={"max_steps": _sl.FINISH_MAX_STEPS}),
    )
    _merge_pass(result, _fin)
    p.emit("step_limit_finish_done",
           aborted=_fin.aborted,
           abort_reason=_fin.abort_reason,
           tool_calls=_fin.tool_calls_total)
    return not _fin.aborted


def _cleanup_turn(p: _Pipeline) -> None:
    """LUXE_CLEANUP_TURN (default ON since 2026-10-18; only the exact
    string "0" disables it): one tool-less call that lets the model
    drop scratch files it created, before the commit. It reaches the
    model only when a candidate exists — 0 of 30 champion runs had one
    (acceptance/tuning_2026_10/REPORT.md §6, §9). luxe/cleanup.py."""
    if not (p.writes
            and os.environ.get("LUXE_CLEANUP_TURN", "1") != "0"):
        return
    from luxe.cleanup import run_cleanup_turn
    _cl = run_cleanup_turn(p.backend, goal=p.goal, repo=p.repo_path,
                           base_sha=p.prep.base_sha,
                           num_ctx=p.cfg.role("monolith").num_ctx)
    p.emit("cleanup_turn",
           candidates=_cl["candidates"], deleted=_cl["deleted"],
           error=_cl["error"], reply=_cl.get("reply"))
    if _cl["candidates"]:
        console.print(
            f"[dim]· cleanup turn: {len(_cl['candidates'])} candidate(s), "
            f"deleted {_cl['deleted'] or 'none'}"
            f"{' — ' + _cl['error'] if _cl['error'] else ''}[/]")


def _pr_cycle(p: _Pipeline, pr_cfg: PRConfig, watch_ci: bool,
              step_limited: bool) -> None:
    if p.writes:
        _open_pr(p, pr_cfg, watch_ci, step_limited)
    elif p.task in {"review", "summarize"}:
        console.print("\n[dim](read-only task; no PR)[/]")


def _open_pr(p: _Pipeline, pr_cfg: PRConfig, watch_ci: bool,
             step_limited: bool) -> None:
    """`pr.open_pr`; no mutations exits 4, any other PR failure exits 5
    (resumable with `luxe pr <run_id>`)."""
    try:
        pr_state = pr_mod.open_pr(
            p.spec,
            report_text=p.report,
            task_type=p.task,
            goal=p.goal,
            test_command=p.prep.test_command,
            branch_name=p.prep.branch_name,
            cfg=pr_cfg,
            watch_ci=watch_ci,
            on_event=lambda kind, data: console.print(
                f"[dim]· pr {kind}: {data}[/]"
            ),
            incomplete=step_limited,
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
        console.print(f"[dim]Resume with: luxe pr {p.spec.run_id}[/]")
        sys.exit(5)


def _emit_report(p: _Pipeline, save_report: bool, output_dir: str) -> None:
    if save_report and p.report:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        report_path = out / f"report_{p.spec.run_id}.md"
        report_path.write_text(p.report)
        console.print(f"[dim]Report also saved: {report_path}[/]")

    if p.report:
        console.print(f"\n{'='*60}")
        console.print(p.report)


# --- teardown -----------------------------------------------------------

def _teardown(mcp_mgr: Any, keep_loaded: bool, lock: Any) -> None:
    """Runs however the guarded block ends (including the PR exits): close
    MCP, drop the indices, unload the model unless --keep-loaded, release
    the repo lock."""
    from luxe import search as search_mod
    from luxe import symbols as symbols_mod
    if mcp_mgr is not None:
        try:
            mcp_mgr.close()
        except Exception:
            pass
    search_mod.reset_index()
    symbols_mod.reset_index()
    if not keep_loaded:
        _unload_models()
    try:
        lock.__exit__(None, None, None)
    except Exception:
        pass


def _unload_models() -> None:
    try:
        from luxe.backend import Backend as _UnloadBackend
        with _UnloadBackend(model="(unload-probe)") as _ub:
            results = _ub.unload_all_loaded()
        if results:
            n_ok = sum(1 for v in results.values() if v)
            console.print(
                f"[dim]· Unloaded {n_ok}/{len(results)} model(s) "
                f"from oMLX (use --keep-loaded to skip)[/]"
            )
    except Exception as e:
        console.print(f"[dim]· Model unload skipped: {e}[/]")
