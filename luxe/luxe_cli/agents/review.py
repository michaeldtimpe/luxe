"""Code review agent — read-only fs surface, review-flavored prompt.

Driven primarily by /review. Distinct from `code` because it never writes:
its job is to enumerate flaws, not apply fixes.
"""

from __future__ import annotations

from typing import Any, Callable

from harness.backends import Backend

from luxe_cli.agents.base import AgentResult, run_agent
from luxe_cli.registry import AgentConfig
from luxe_cli.session import Session
from luxe_cli.tasks.cache import ToolCache, wrap_tool_fns
from luxe_cli.tools import analysis, fs, git_tools


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
    tool_cache: ToolCache | None = None,
) -> AgentResult:
    tool_defs = (
        list(fs.read_only_defs())
        + list(git_tools.tool_defs())
        + list(analysis.tool_defs(languages=cfg.analyzer_languages))
    )
    tool_fns = {**fs.READ_ONLY_FNS, **git_tools.TOOL_FNS, **analysis.TOOL_FNS}

    if tool_cache is not None:
        cacheable = (
            set(fs.READ_ONLY_FNS)
            | set(git_tools.TOOL_FNS)
            | set(analysis.TOOL_FNS)
        )
        tool_fns = wrap_tool_fns(tool_fns, tool_cache, cacheable)

    return run_agent(
        backend,
        cfg,
        task=task,
        tool_defs=tool_defs,
        tool_fns=tool_fns,
        session=session,
        on_tool_event=on_tool_event,
    )
