"""Refactor-suggestion agent — read-only fs surface, optimization-flavored prompt.

Same pattern as review: enumerates opportunities, doesn't apply them. A
future /refactor --apply mode can layer write tools on top if needed.
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
    tool_fns = dict(fs.READ_ONLY_FNS)

    return run_agent(
        backend,
        cfg,
        task=task,
        tool_defs=tool_defs,
        tool_fns=tool_fns,
        session=session,
    )
