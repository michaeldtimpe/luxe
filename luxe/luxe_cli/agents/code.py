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

from typing import Any, Callable

from harness.backends import Backend

from luxe_cli.agents.base import AgentResult, run_agent
from luxe_cli.registry import AgentConfig
from luxe_cli.session import Session
from luxe_cli.tasks.cache import ToolCache, wrap_tool_fns
from luxe_cli.tools import analysis, fs, shell, web


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
    read_only: bool = False,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
    tool_cache: ToolCache | None = None,
) -> AgentResult:
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

    if tool_cache is not None:
        # Mutations (write_file / edit_file) and shell calls are
        # deliberately NOT in the cacheable set — their behavior depends
        # on (and changes) filesystem state. Web fetches are also
        # excluded: URLs can change between calls and the cost of a
        # duplicate fetch is usually network, not compute.
        cacheable = set(fs.READ_ONLY_FNS) | set(analysis.TOOL_FNS)
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
