"""Single-mode agent — one capable model, full tool surface, agentic loop.

Used for smaller repos and quick fixes where the swarm pipeline would be
overkill. Reuses agents/loop.py:run_agent — the only difference from a worker
is the system prompt (frames the task end-to-end) and the tool surface (full
read+write+analyze+shell+git, plus MCP tools when injected).

Emits an `escalate_to_swarm` signal in `final_text` if the model determines
the task needs >10 file edits or sustained multi-component planning. The CLI
detects that signal and re-launches as swarm with an EscalationContext.
"""

from __future__ import annotations

from luxe.agents.loop import AgentResult, OnToolEvent, run_agent
from luxe.backend import Backend
from luxe.config import RoleConfig
from luxe.tools import analysis, fs, git, shell
from luxe.tools.base import ToolCache, ToolDef, ToolFn
from luxe import search as search_mod
from luxe import symbols as symbols_mod

ESCALATE_SIGNAL = "escalate_to_swarm"

_SYSTEM_PROMPT = """\
You are a code maintenance specialist working on a single repository. Your job
is to take a goal end-to-end: read what's relevant, plan the change, edit code
when needed, run tests if available, and produce a final report.

Operating principles:
- Read first. Understand the repo before you edit it.
- Make minimal, focused changes — only what the goal requires.
- Cite every file you read with file:path syntax; cite every file you modify.
- Preserve existing style and conventions.
- When you finish, output a final report summarising what you changed,
  what tests you ran, and any open questions.

Escalation:
- If you determine this task needs more than 10 file edits, multi-component
  planning, or systematic decomposition you cannot hold in one context window,
  STOP making changes and emit a single line containing the literal string
  "{escalate}" followed by a brief reason. Do NOT continue piecemeal — escalation
  is not a failure, it routes the task to the multi-stage swarm pipeline.
- If you have already made coherent partial changes, summarise them in your
  last assistant message before emitting "{escalate}" — your tool-call history
  becomes the swarm architect's seed context.

Citation contract:
- Every file:line citation in your final report MUST resolve in the current
  repo state. The post-synthesis citation linter will verify each one.
- If you cite a line in a file you also edited, include a 1–3 line snippet of
  the cited code verbatim alongside the citation; the linter uses fuzzy snippet
  matching to forgive line-shift after edits.
""".format(escalate=ESCALATE_SIGNAL)


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


def did_escalate(result: AgentResult) -> bool:
    """True if the single-mode result indicates the model wants swarm."""
    return ESCALATE_SIGNAL in (result.final_text or "")


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

    task_prompt = (
        f"Task type: {task_type}\n"
        f"Goal: {goal}\n\n"
        "Begin by reading what's relevant to plan your change. "
        "When you're done, end with a final report."
    )

    return run_agent(
        backend, role_cfg,
        system_prompt=_SYSTEM_PROMPT,
        task_prompt=task_prompt,
        tool_defs=defs,
        tool_fns=fns,
        cache=cache,
        cacheable=cacheable,
        on_tool_event=on_tool_event,
    )
