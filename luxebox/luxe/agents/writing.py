"""Creative writing agent.

Higher temperature, long-form prompt. Gets the fs surface (read + write)
scoped to the folder `luxe` was started from, so it can review existing
documents, draft new ones to disk, and revise in place.
"""

from __future__ import annotations

from harness.backends import Backend

from luxe.agents.base import AgentResult, run_agent
from luxe.registry import AgentConfig
from luxe.session import Session
from luxe.tools import fs


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
) -> AgentResult:
    tool_defs = list(fs.read_only_defs())
    tool_defs.extend(fs.mutation_defs())
    tool_fns = dict(fs.READ_ONLY_FNS)
    tool_fns.update(fs.MUTATION_FNS)

    return run_agent(
        backend,
        cfg,
        task=task,
        tool_defs=tool_defs,
        tool_fns=tool_fns,
        session=session,
    )
