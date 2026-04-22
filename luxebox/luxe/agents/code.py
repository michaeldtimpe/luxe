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
from luxe.session import Session
from luxe.tools import fs, shell, web


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
    read_only: bool = False,
) -> AgentResult:
    tool_defs = list(fs.read_only_defs())
    tool_fns = dict(fs.READ_ONLY_FNS)
    tool_defs.extend(web.tool_defs())
    tool_fns.update(web.TOOL_FNS)

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
