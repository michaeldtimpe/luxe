"""Code review agent — read-only fs surface, review-flavored prompt.

Driven primarily by /review. Distinct from `code` because it never writes:
its job is to enumerate flaws, not apply fixes.
"""

from __future__ import annotations

from harness.backends import Backend

from luxe.agents.base import AgentResult, run_agent
from luxe.registry import AgentConfig
from luxe.session import Session
from luxe.tools import fs, git_tools


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
) -> AgentResult:
    tool_defs = list(fs.read_only_defs()) + list(git_tools.tool_defs())
    tool_fns = {**fs.READ_ONLY_FNS, **git_tools.TOOL_FNS}

    return run_agent(
        backend,
        cfg,
        task=task,
        tool_defs=tool_defs,
        tool_fns=tool_fns,
        session=session,
    )
