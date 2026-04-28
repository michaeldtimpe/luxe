"""Synthesizer agent — assembles validated findings into a final report."""

from __future__ import annotations

from luxe.agents.loop import AgentResult, run_agent
from luxe.backend import Backend
from luxe.config import RoleConfig

_SYSTEM_PROMPT_REVIEW = """\
You are a report synthesizer. Assemble the validated findings into a clear,
severity-grouped report.

Output format:
# Code Review Report

## Critical Issues
(findings that must be fixed before merge)

## High Priority
(significant bugs or security issues)

## Medium Priority
(code quality, performance, maintainability)

## Low Priority / Suggestions
(style, minor improvements)

## Summary
- Total findings: N
- Critical: N | High: N | Medium: N | Low: N
- Files analyzed: (list)
- Key recommendations: (2-3 bullet points)

Rules:
- Deduplicate findings that describe the same issue
- Preserve file:line citations from the validated findings
- Classify severity based on impact and likelihood
- Be concise — one paragraph per finding maximum
- Do not fabricate findings or citations
"""

_SYSTEM_PROMPT_IMPLEMENT = """\
You are an implementation summarizer. Assemble the work done by specialist workers
into a clear summary of changes made.

Output format:
# Implementation Summary

## Changes Made
(list each file changed with description)

## Testing
(any tests run and their results)

## Remaining Work
(anything that couldn't be completed)

## Notes
(important context for reviewers)
"""

_SYSTEM_PROMPT_DEFAULT = """\
You are a report synthesizer. Assemble the findings from specialist workers
into a clear, well-organized report. Preserve all file:line citations.
Deduplicate where appropriate. Be concise.
"""

_TASK_TYPE_PROMPTS = {
    "review": _SYSTEM_PROMPT_REVIEW,
    "bugfix": _SYSTEM_PROMPT_REVIEW,
    "implement": _SYSTEM_PROMPT_IMPLEMENT,
    "document": _SYSTEM_PROMPT_DEFAULT,
    "summarize": _SYSTEM_PROMPT_DEFAULT,
    "manage": _SYSTEM_PROMPT_DEFAULT,
}


def run_synthesizer(
    backend: Backend,
    role_cfg: RoleConfig,
    *,
    validated_findings: str,
    task_type: str = "review",
    goal: str = "",
) -> AgentResult:
    """Assemble validated findings into a final report. No tools — pure synthesis."""

    system_prompt = _TASK_TYPE_PROMPTS.get(task_type, _SYSTEM_PROMPT_DEFAULT)

    task_parts = []
    if goal:
        task_parts.append(f"Original goal: {goal}")
    task_parts.extend([
        "",
        "Synthesize the following validated findings into a final report:",
        "",
        validated_findings,
    ])

    return run_agent(
        backend, role_cfg,
        system_prompt=system_prompt,
        task_prompt="\n".join(task_parts),
        tool_defs=[],
        tool_fns={},
    )
