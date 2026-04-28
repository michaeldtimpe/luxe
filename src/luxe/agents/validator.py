"""Validator agent — verifies citations and findings from workers."""

from __future__ import annotations

from luxe.agents.loop import AgentResult, OnToolEvent, run_agent
from luxe.backend import Backend
from luxe.config import RoleConfig
from luxe.tools import fs, git
from luxe.tools.base import ToolCache

_SYSTEM_PROMPT = """\
You are a citation verifier. Your job is to verify findings from code analysis workers.

For each finding with a file:line citation:
1. Use read_file to confirm the file exists
2. Read the cited lines to confirm the code matches what was claimed
3. Use grep if needed to verify patterns across files

Output rules:
- Keep findings you can verify — include the original finding text
- Remove findings where the file doesn't exist or the cited code doesn't match
- Flag findings you cannot fully verify with [UNVERIFIED]
- Do NOT add new findings — only verify or remove existing ones
- Preserve the original severity classifications

Output format:
## Verified Findings
(list verified findings with original citations)

## Removed Findings
(list findings that failed verification, with reason)

## Verification Summary
- Total findings checked: N
- Verified: N
- Removed: N
- Unverified (kept with flag): N
"""


def run_validator(
    backend: Backend,
    role_cfg: RoleConfig,
    *,
    worker_findings: str,
    cache: ToolCache | None = None,
    on_tool_event: OnToolEvent | None = None,
) -> AgentResult:
    """Verify worker findings by checking citations against actual code."""

    defs = fs.read_only_defs()[:1] + [  # read_file only
        d for d in fs.read_only_defs() if d.name == "grep"
    ] + git.tool_defs()[:1]  # git_diff for freshness checks

    fns = {
        "read_file": fs.READ_ONLY_FNS["read_file"],
        "grep": fs.READ_ONLY_FNS["grep"],
    }
    cacheable = {"read_file", "grep"}

    task_prompt = (
        "Verify the following findings from code analysis workers. "
        "Check every file:line citation.\n\n"
        f"{worker_findings}"
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
