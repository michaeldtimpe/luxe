"""Architect agent — decomposes goals into micro-objectives with role tags."""

from __future__ import annotations

import json
from typing import Any

from luxe.agents.loop import AgentResult, run_agent
from luxe.backend import Backend
from luxe.config import RoleConfig

_SYSTEM_PROMPT = """\
You are a task architect for a specialist swarm pipeline. Your job is to decompose
a user goal into focused micro-objectives that specialist workers can execute.

Each micro-objective must specify:
- "title": A clear, actionable one-line description
- "role": One of: worker_read, worker_code, worker_analyze
- "expected_tools": Estimated number of tool calls needed (max 5)
- "scope": File or module hint to focus the worker

Rules:
- Each worker micro-objective should require at most 5 tool calls
- Security/lint/typecheck tasks → worker_analyze
- File reading, grep, structure exploration → worker_read
- Code writing, editing, file creation → worker_code
- Do NOT include validator or synthesizer entries — those are added automatically
- Produce 4-12 micro-objectives, ordered logically (reads before writes)
- Prefer more focused tasks over fewer broad ones

Respond with ONLY a JSON array. No markdown, no explanation.

Example output:
[
  {"title": "Survey repo structure and identify entry points", "role": "worker_read", "expected_tools": 3, "scope": "."},
  {"title": "Run security scanner on auth module", "role": "worker_analyze", "expected_tools": 2, "scope": "src/auth/"},
  {"title": "Implement input validation for user endpoints", "role": "worker_code", "expected_tools": 4, "scope": "src/api/users.py"}
]
"""


def run_architect(
    backend: Backend,
    role_cfg: RoleConfig,
    *,
    goal: str,
    task_type_prompt: str,
    repo_summary: str = "",
) -> tuple[AgentResult, list[dict[str, Any]]]:
    """Run the architect and parse micro-objectives from its output.

    Returns (agent_result, micro_objectives).
    """
    task_prompt_parts = [task_type_prompt, "", f"Goal: {goal}"]
    if repo_summary:
        task_prompt_parts.extend(["", f"Repository summary:\n{repo_summary}"])

    result = run_agent(
        backend, role_cfg,
        system_prompt=_SYSTEM_PROMPT,
        task_prompt="\n".join(task_prompt_parts),
        tool_defs=[],
        tool_fns={},
    )

    objectives = _parse_objectives(result.final_text)
    return result, objectives


def _parse_objectives(text: str) -> list[dict[str, Any]]:
    """Extract JSON array of micro-objectives from architect output."""
    text = text.strip()

    # Strip markdown fences
    if text.startswith("```"):
        lines = text.splitlines()
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    # Find the JSON array
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        return _fallback_single(text)

    try:
        arr = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return _fallback_single(text)

    if not isinstance(arr, list):
        return _fallback_single(text)

    valid = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        if "title" not in item:
            continue
        role = item.get("role", "worker_read")
        if role not in {"worker_read", "worker_code", "worker_analyze"}:
            role = "worker_read"
        valid.append({
            "title": item["title"],
            "role": role,
            "expected_tools": item.get("expected_tools", 3),
            "scope": item.get("scope", "."),
        })

    return valid if valid else _fallback_single(text)


def _fallback_single(text: str) -> list[dict[str, Any]]:
    """Fallback: treat the entire goal as a single worker_read task."""
    return [{
        "title": "Execute goal (architect failed to decompose)",
        "role": "worker_read",
        "expected_tools": 5,
        "scope": ".",
    }]
