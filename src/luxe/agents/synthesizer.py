"""Synthesizer agent — assembles validated findings into a final report.

Contract changes (rev 2):
- Input: ValidatorEnvelope (structured), not free-form text.
- Output: a markdown report that preserves every `path:line` AND `snippet`
  from the validator's verified findings, verbatim. The post-synthesis
  citation linter forbids new citations and uses the snippets to fuzzy-match
  against post-edit repo state.
- For `cleared` status: produce a "no issues found" report.
- For `ambiguous` status: include a top-of-report warning paragraph.
"""

from __future__ import annotations

from luxe.agents.loop import AgentResult, run_agent
from luxe.agents.validator import ValidatorEnvelope, ValidatorFinding
from luxe.backend import Backend
from luxe.config import RoleConfig


_CITATION_CONTRACT = """\
CITATION CONTRACT (load-bearing — do not violate):
- You MUST NOT introduce new file:line citations. Use ONLY citations present
  in the validated findings provided to you.
- For each finding you include, preserve the exact `path:line` token verbatim
  AND quote the `snippet` (1–3 lines of code) directly beneath it as a
  fenced code block. The post-synthesis linter uses the snippet to verify
  the citation against the current repo state with fuzzy line-matching.
- Do not paraphrase code. If you reference code, quote it verbatim.
"""

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
- Files cited: (list)
- Key recommendations: (2-3 bullet points)

For each finding render:

### {severity}: {short title}
**Citation:** `{path}:{line}`
```
{snippet verbatim from validator}
```
{description}

""" + _CITATION_CONTRACT

_SYSTEM_PROMPT_IMPLEMENT = """\
You are an implementation summarizer. Assemble the work done by specialist
workers into a clear summary of changes made.

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

""" + _CITATION_CONTRACT

_SYSTEM_PROMPT_DEFAULT = """\
You are a report synthesizer. Assemble the findings from specialist workers
into a clear, well-organized report. Deduplicate where appropriate. Be concise.

""" + _CITATION_CONTRACT

_TASK_TYPE_PROMPTS = {
    "review": _SYSTEM_PROMPT_REVIEW,
    "bugfix": _SYSTEM_PROMPT_REVIEW,
    "implement": _SYSTEM_PROMPT_IMPLEMENT,
    "document": _SYSTEM_PROMPT_DEFAULT,
    "summarize": _SYSTEM_PROMPT_DEFAULT,
    "manage": _SYSTEM_PROMPT_DEFAULT,
}


def _render_finding(f: ValidatorFinding) -> str:
    parts = [
        f"### {f.severity.upper()}: {f.description.split(chr(10))[0][:120]}",
        f"**Citation:** `{f.path}:{f.line}`",
        "```",
        f.snippet,
        "```",
        f.description,
    ]
    return "\n".join(parts)


def _render_envelope_for_synthesis(envelope: ValidatorEnvelope) -> str:
    """Format the structured envelope as the task prompt body."""
    lines: list[str] = []
    lines.append(f"Validator status: **{envelope.status}**")
    if envelope.summary:
        lines.append(f"Summary: {envelope.summary}")
    lines.append("")

    if envelope.is_cleared:
        lines.append(
            "No findings to report — workers raised nothing requiring "
            "verification. Produce a brief 'no issues found' report."
        )
        return "\n".join(lines)

    if envelope.is_ambiguous:
        lines.append(
            "WARNING: this run had elevated unverifiable findings (more than "
            "half were removed during validation). Begin your report with the "
            "following warning paragraph verbatim:"
        )
        lines.append(
            "> **Note:** This run had elevated unverifiable findings during "
            "validation. The findings below were verified, but the high "
            "removal rate suggests the workers may have hallucinated. Consider "
            "re-running with `--mode swarm` (if you used single mode) or "
            "having a human review the flagged areas."
        )
        lines.append("")

    if envelope.verified:
        lines.append("## Validated findings (use ONLY these citations and snippets)")
        for f in envelope.verified:
            lines.append("")
            lines.append(_render_finding(f))
    else:
        lines.append("(No findings survived validation.)")

    return "\n".join(lines)


def run_synthesizer(
    backend: Backend,
    role_cfg: RoleConfig,
    *,
    envelope: ValidatorEnvelope,
    task_type: str = "review",
    goal: str = "",
) -> AgentResult:
    """Assemble validated findings into a final report. No tools — pure synthesis."""

    system_prompt = _TASK_TYPE_PROMPTS.get(task_type, _SYSTEM_PROMPT_DEFAULT)

    task_parts: list[str] = []
    if goal:
        task_parts.append(f"Original goal: {goal}")
    task_parts.append("")
    task_parts.append("Synthesize the following validator output into a final report:")
    task_parts.append("")
    task_parts.append(_render_envelope_for_synthesis(envelope))

    return run_agent(
        backend, role_cfg,
        system_prompt=system_prompt,
        task_prompt="\n".join(task_parts),
        tool_defs=[],
        tool_fns={},
    )
