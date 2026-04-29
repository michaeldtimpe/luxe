"""Worker agents — role-specific tool surfaces for read, code, and analyze tasks."""

from __future__ import annotations

from typing import Any

from luxe.agents.loop import AgentResult, OnToolEvent, run_agent
from luxe.backend import Backend
from luxe.config import RoleConfig
from luxe.tools.base import ToolCache, ToolDef, ToolFn
from luxe.tools import fs, git, analysis, shell
from luxe import search as search_mod
from luxe import symbols as symbols_mod

_WORKER_READ_PROMPT = """\
You are a code reader specialist. Your job is to gather specific information from
a codebase by reading files, searching patterns, and inspecting git history.

Rules:
- Be focused and efficient — each tool call should serve a clear purpose
- Report your findings as structured observations with file:line citations
- Do not make changes to any files
- If you cannot find what you're looking for, say so clearly
"""

_WORKER_CODE_PROMPT = """\
You are a code implementation specialist. Your job is to write, edit, or create
code files to fulfill a specific objective.

Rules:
- Read relevant code first to understand context before making changes
- Make minimal, focused changes — only what the objective requires
- Cite every file you modify with its path
- Test your changes if possible (run linters, type checkers)
- Do not refactor beyond what the objective asks for
"""

_WORKER_ANALYZE_PROMPT = """\
You are a code analysis specialist. Your job is to run static analysis tools
and inspect code for bugs, security issues, and quality problems.

Rules:
- Use the appropriate analysis tools for the language (lint, typecheck, security_scan)
- For each finding, verify it by reading the relevant code
- Report findings with exact file:line citations
- Classify severity: critical, high, medium, low, info
- Do not make changes to any files
"""

_ROLE_PROMPTS = {
    "worker_read": _WORKER_READ_PROMPT,
    "worker_code": _WORKER_CODE_PROMPT,
    "worker_analyze": _WORKER_ANALYZE_PROMPT,
}


def _build_tools_for_role(
    role: str,
    languages: frozenset[str] | None = None,
) -> tuple[list[ToolDef], dict[str, ToolFn], set[str]]:
    """Assemble tool defs and fns for a worker role."""
    defs: list[ToolDef] = []
    fns: dict[str, ToolFn] = {}
    cacheable: set[str] = set()

    if role in ("worker_read", "worker_code", "worker_analyze"):
        defs.extend(fs.read_only_defs())
        fns.update(fs.READ_ONLY_FNS)
        cacheable.update(fs.CACHEABLE)
        # BM25 and AST symbol search are universal read-only retrieval tools.
        defs.append(search_mod.bm25_search_def())
        fns.update(search_mod.TOOL_FNS)
        cacheable.update(search_mod.CACHEABLE)
        defs.append(symbols_mod.find_symbol_def())
        fns.update(symbols_mod.TOOL_FNS)
        cacheable.update(symbols_mod.CACHEABLE)

    if role in ("worker_read", "worker_code", "worker_analyze"):
        defs.extend(git.tool_defs())
        fns.update(git.TOOL_FNS)
        cacheable.update(git.CACHEABLE)

    if role == "worker_code":
        defs.extend(fs.mutation_defs())
        fns.update(fs.MUTATION_FNS)
        defs.extend(shell.tool_defs())
        fns.update(shell.TOOL_FNS)

    if role == "worker_analyze":
        analysis_defs = analysis.tool_defs(languages)
        analysis_fns = analysis.tool_fns(languages)
        defs.extend(analysis_defs)
        fns.update(analysis_fns)
        cacheable.update(analysis.CACHEABLE)

    return defs, fns, cacheable


def run_worker(
    backend: Backend,
    role_cfg: RoleConfig,
    *,
    role: str,
    task_prompt: str,
    prior_findings: str = "",
    languages: frozenset[str] | None = None,
    extra_tool_defs: list[ToolDef] | None = None,
    extra_tool_fns: dict[str, ToolFn] | None = None,
    cache: ToolCache | None = None,
    on_tool_event: OnToolEvent | None = None,
) -> AgentResult:
    """Run a worker agent with role-appropriate tools.

    `extra_tool_defs/extra_tool_fns` (e.g. MCP-discovered tools) are appended
    to the role's native tool surface. They are NOT added to the cacheable
    set — external tools may be stateful or non-deterministic, so caching
    would be unsafe.
    """

    system_prompt = _ROLE_PROMPTS.get(role, _WORKER_READ_PROMPT)
    defs, fns, cacheable_set = _build_tools_for_role(role, languages)

    if extra_tool_defs:
        defs = defs + list(extra_tool_defs)
    if extra_tool_fns:
        fns = {**fns, **extra_tool_fns}

    full_prompt_parts = [task_prompt]
    if prior_findings:
        full_prompt_parts.extend([
            "",
            "## Prior findings from earlier pipeline stages",
            prior_findings,
        ])

    return run_agent(
        backend, role_cfg,
        system_prompt=system_prompt,
        task_prompt="\n".join(full_prompt_parts),
        tool_defs=defs,
        tool_fns=fns,
        cache=cache,
        cacheable=cacheable_set,
        on_tool_event=on_tool_event,
    )
