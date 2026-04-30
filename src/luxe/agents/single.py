"""Single-mode (mono) agent — one capable model, full tool surface, agentic loop.

The only execution mode in v1.0. Reuses agents/loop.py:run_agent; the
distinguishing pieces are the system prompt (frames the task end-to-end)
and the tool surface (full read+write+analyze+shell+git, plus MCP tools
when injected).

Prompts come from src/luxe/agents/prompts.py via RoleConfig.system_prompt_id
and RoleConfig.task_prompt_id; see that module's docstring for the editing
norm.
"""

from __future__ import annotations

from luxe.agents.loop import AgentResult, OnToolEvent, run_agent
from luxe.agents.prompts import get as get_prompt
from luxe.backend import Backend
from luxe.config import RoleConfig
from luxe.tools import analysis, fs, git, shell
from luxe.tools.base import ToolCache, ToolDef, ToolFn
from luxe import search as search_mod
from luxe import symbols as symbols_mod


def _build_full_tool_surface(
    languages: frozenset[str] | None,
    tool_allowlist: list[str] | None,
) -> tuple[list[ToolDef], dict[str, ToolFn], set[str]]:
    """Assemble the full read+write+analyze+shell+git tool surface.

    `tool_allowlist` (typically from the role config) restricts which of these
    are exposed. Pass None to expose everything — handy for tests.
    """
    defs: list[ToolDef] = []
    fns: dict[str, ToolFn] = {}
    cacheable: set[str] = set()

    defs.extend(fs.read_only_defs())
    fns.update(fs.READ_ONLY_FNS)
    cacheable.update(fs.CACHEABLE)

    defs.append(search_mod.bm25_search_def())
    fns.update(search_mod.TOOL_FNS)
    cacheable.update(search_mod.CACHEABLE)

    defs.append(symbols_mod.find_symbol_def())
    fns.update(symbols_mod.TOOL_FNS)
    cacheable.update(symbols_mod.CACHEABLE)

    defs.extend(fs.mutation_defs())
    fns.update(fs.MUTATION_FNS)

    defs.extend(git.tool_defs())
    fns.update(git.TOOL_FNS)
    cacheable.update(git.CACHEABLE)

    defs.extend(shell.tool_defs())
    fns.update(shell.TOOL_FNS)

    a_defs = analysis.tool_defs(languages)
    a_fns = analysis.tool_fns(languages)
    defs.extend(a_defs)
    fns.update(a_fns)
    cacheable.update(analysis.CACHEABLE)

    if tool_allowlist is not None:
        allowed = set(tool_allowlist)
        defs = [d for d in defs if d.name in allowed]
        fns = {n: f for n, f in fns.items() if n in allowed}
        cacheable = cacheable & allowed

    return defs, fns, cacheable


def run_single(
    backend: Backend,
    role_cfg: RoleConfig,
    *,
    goal: str,
    task_type: str = "review",
    languages: frozenset[str] | None = None,
    extra_tool_defs: list[ToolDef] | None = None,
    extra_tool_fns: dict[str, ToolFn] | None = None,
    cache: ToolCache | None = None,
    on_tool_event: OnToolEvent | None = None,
) -> AgentResult:
    """Run the single-mode agent end-to-end on a goal.

    `role_cfg.tools` (from configs/single_64gb.yaml's `monolith` role) is the
    allowlist of native tools. Anything in `extra_tool_defs` (e.g. MCP tools)
    is appended unconditionally — MCP tools are namespaced and can't collide.
    """
    defs, fns, cacheable = _build_full_tool_surface(languages, role_cfg.tools or None)

    if extra_tool_defs:
        defs = defs + list(extra_tool_defs)
    if extra_tool_fns:
        fns = {**fns, **extra_tool_fns}

    sys_variant = get_prompt(role_cfg.system_prompt_id)
    task_variant = get_prompt(role_cfg.task_prompt_id)
    task_prompt = (
        f"Task type: {task_type}\n"
        f"Goal: {goal}\n\n"
        f"{task_variant.task_prefix}"
    )

    return run_agent(
        backend, role_cfg,
        system_prompt=sys_variant.system,
        task_prompt=task_prompt,
        tool_defs=defs,
        tool_fns=fns,
        cache=cache,
        cacheable=cacheable,
        on_tool_event=on_tool_event,
    )
