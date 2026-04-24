"""Calculation / estimation agent.

Gets two sources of tools:
  1. `create_tool` — so it can save reusable computations for future
     tasks (EV charging math, mortgage payments, tip splits, etc.).
  2. The user's saved tool library — matching tools get auto-injected
     into the agent's tool set based on task keywords, so the second
     EV trip doesn't re-derive the formula from scratch.

Dedicated because `general` on a small 7B model hallucinates arithmetic
and occasionally refuses benign cost/time questions. A mid-sized MoE
model handles multi-step math much more reliably.
"""

from __future__ import annotations

from typing import Any, Callable

from harness.backends import Backend

from cli.agents.base import AgentResult, run_agent
from cli.registry import AgentConfig
from cli.session import Session
from cli.tool_library import (
    CREATE_TOOL_DEF,
    create_tool_fn,
    match_tools,
    tool_def_from_meta,
    tool_fn_from_meta,
)


def _library_hint(matched: list[dict]) -> str:
    """Prepend a short catalog of auto-injected tools to the system
    prompt so the model knows to call them by name rather than re-deriving."""
    if not matched:
        return ""
    lines = [
        "",
        "# Tools from your saved library (auto-matched to this task)",
        "# Prefer calling these by name over recomputing from scratch.",
    ]
    for m in matched:
        lines.append(f"- `{m['name']}`: {m.get('description', '')}")
    return "\n".join(lines) + "\n"


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
) -> AgentResult:
    matched = match_tools(task, limit=4)
    tool_defs = [CREATE_TOOL_DEF, *(tool_def_from_meta(m) for m in matched)]
    tool_fns: dict = {"create_tool": create_tool_fn}
    for m in matched:
        tool_fns[m["name"]] = tool_fn_from_meta(m)

    hint = _library_hint(matched)
    if hint:
        cfg = cfg.model_copy(update={"system_prompt": cfg.system_prompt + hint})

    return run_agent(
        backend,
        cfg,
        task=task,
        tool_defs=tool_defs,
        tool_fns=tool_fns,
        session=session,
        on_tool_event=on_tool_event,
    )
