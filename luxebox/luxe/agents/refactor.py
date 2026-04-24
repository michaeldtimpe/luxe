"""Refactor-suggestion agent — read-only fs surface, optimization-flavored prompt.

Same pattern as review: enumerates opportunities, doesn't apply them. A
future /refactor --apply mode can layer write tools on top if needed.
"""

from __future__ import annotations

from typing import Any, Callable

from harness.backends import Backend

from luxe.agents.base import AgentResult, run_agent
from luxe.registry import AgentConfig
from luxe.session import Session
from luxe.tools import analysis, fs, git_tools


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
) -> AgentResult:
    tool_defs = (
        list(fs.read_only_defs())
        + list(git_tools.tool_defs())
        + list(analysis.tool_defs(languages=cfg.analyzer_languages))
    )
    tool_fns = {**fs.READ_ONLY_FNS, **git_tools.TOOL_FNS, **analysis.TOOL_FNS}

    return run_agent(
        backend,
        cfg,
        task=task,
        tool_defs=tool_defs,
        tool_fns=tool_fns,
        session=session,
        on_tool_event=on_tool_event,
    )
