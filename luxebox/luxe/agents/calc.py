"""Calculation / estimation agent — no tools, just numeric reasoning.

Dedicated because `general` on a small 7B model hallucinates arithmetic
and occasionally refuses benign cost/time questions. A mid-sized MoE or
coder model handles multi-step math much more reliably.
"""

from __future__ import annotations

from harness.backends import Backend

from luxe.agents.base import AgentResult, run_agent
from luxe.registry import AgentConfig
from luxe.session import Session


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
) -> AgentResult:
    return run_agent(
        backend,
        cfg,
        task=task,
        tool_defs=[],
        tool_fns={},
        session=session,
    )
