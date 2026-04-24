"""Coding agent — full Claude-Code-like tool surface, scoped to a repo root.

Modes:
- `read_only=True`: analysis/review only (read_file, list_dir, glob, grep).
  Nothing is written. Used for the analyze-repo flow.
- `read_only=False`: full surface, adds write_file, edit_file, bash.
  Used for actual refactor/bugfix/feature work.

Either way, all fs operations are confined to `fs.repo_root()` and bash
to the shell allowlist.
"""

from __future__ import annotations

from harness.backends import Backend

from luxe.agents.base import AgentResult, run_agent
from luxe.registry import AgentConfig
from luxe.repo_survey import analyze_repo, size_budgets
from luxe.session import Session
from luxe.tools import analysis, fs, shell, web


def _resize_for_cwd(cfg: AgentConfig) -> AgentConfig:
    """Pre-flight survey the agent's cwd and bump `num_ctx`/`max_wall_s`
    when the repo is medium+. On tiny/small repos the agent's static
    config is fine; on larger codebases the default 8k context window
    is the bottleneck that keeps the agent re-reading files instead of
    holding them in view. 14B at 16k ctx is the same shape as /review
    uses after Phase 7, just per-dispatch instead of per-task."""
    try:
        survey = analyze_repo(fs.repo_root())
    except Exception:  # noqa: BLE001
        return cfg
    decision = size_budgets(survey)
    if decision.tier in ("tiny", "small"):
        return cfg
    updates: dict[str, float | int] = {}
    # Only bump if the result is actually larger than current config.
    if cfg.num_ctx is None or cfg.num_ctx < decision.num_ctx:
        updates["num_ctx"] = decision.num_ctx
    if cfg.max_wall_s < decision.task_max_wall_s / 2:
        # The code agent is a single-turn dispatch (no task wrapper), so
        # scale its wall to half the task-tier budget — plenty for a
        # single interactive step without burning an hour on one call.
        updates["max_wall_s"] = float(decision.task_max_wall_s / 2)
    if not updates:
        return cfg
    return cfg.model_copy(update=updates)


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
    read_only: bool = False,
) -> AgentResult:
    cfg = _resize_for_cwd(cfg)
    tool_defs = list(fs.read_only_defs())
    tool_fns = dict(fs.READ_ONLY_FNS)
    tool_defs.extend(web.tool_defs())
    tool_fns.update(web.TOOL_FNS)
    tool_defs.extend(analysis.tool_defs())
    tool_fns.update(analysis.TOOL_FNS)

    if not read_only:
        tool_defs.extend(fs.mutation_defs())
        tool_fns.update(fs.MUTATION_FNS)
        tool_defs.extend(shell.tool_defs())
        tool_fns.update(shell.TOOL_FNS)

    return run_agent(
        backend,
        cfg,
        task=task,
        tool_defs=tool_defs,
        tool_fns=tool_fns,
        session=session,
    )
