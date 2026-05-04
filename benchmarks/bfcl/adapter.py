"""BFCL problem → luxe Backend invocation adapter (raw + agent modes).

PRELIMINARY (2026-05-03). Loads BFCL v4 problems from the installed
`bfcl_eval` package, runs them against the luxe backend, returns
(actual_tool_calls, timing) per problem.

Two modes (per `~/.claude/plans/fancy-honking-lerdorf.md`):

- `raw`: single-turn `backend.chat()` with the BFCL function as a tool.
  Captures the model's first emitted tool calls. Comparable to public
  BFCL numbers (fair model-only baseline).
- `agent`: full `run_agent()` loop with the BFCL spec as the only ToolDef
  and a stub executor. Captures all tool calls from the loop. Measures
  whether luxe's prompt scaffolding helps or hurts.

For irrelevance category: tools are still passed but the model must
correctly NOT call them. Both modes apply.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from luxe.backend import Backend, ChatResponse
from luxe.config import RoleConfig
from luxe.tools.base import ToolDef

from .schemas import bfcl_func_spec_to_tool_def, make_stub_executor


# Categories we run. Subset chosen for Python-relevance and Mode-B parity.
SUPPORTED_CATEGORIES = (
    "simple_python",
    "multiple",
    "parallel",
    "parallel_multiple",
    "irrelevance",
    # multi_turn deferred — needs state-tracking grader.
)


def _bfcl_data_dir() -> Path:
    """Locate the installed bfcl_eval package's data dir."""
    import bfcl_eval
    return Path(bfcl_eval.__file__).parent / "data"


def _category_filename(category: str) -> str:
    return f"BFCL_v4_{category}.json"


def load_problems(category: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Load problems for a category, optionally capped at `limit`."""
    data_dir = _bfcl_data_dir()
    path = data_dir / _category_filename(category)
    if not path.is_file():
        raise FileNotFoundError(f"BFCL category data not found: {path}")
    out: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
            if limit and len(out) >= limit:
                break
    return out


def load_ground_truth(category: str) -> dict[str, list[Any]]:
    """Load ground-truth for a category as a {problem_id: gt_list} dict.
    Returns empty dict for irrelevance (no gt).
    """
    if category == "irrelevance":
        return {}
    data_dir = _bfcl_data_dir()
    path = data_dir / "possible_answer" / _category_filename(category)
    if not path.is_file():
        return {}
    out: dict[str, list[Any]] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            out[entry["id"]] = entry.get("ground_truth", [])
    return out


def _problem_messages(problem: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert BFCL `question` (list-of-list-of-messages) to a flat
    OpenAI-style message list. Single-turn problems wrap the user
    message in `[[{...}]]`; we take the first turn.
    """
    question = problem.get("question") or []
    if not question:
        return [{"role": "user", "content": ""}]
    # First turn's messages.
    first_turn = question[0]
    if not isinstance(first_turn, list):
        first_turn = [first_turn]
    msgs: list[dict[str, Any]] = []
    for m in first_turn:
        if isinstance(m, dict) and "role" in m and "content" in m:
            msgs.append({"role": m["role"], "content": m["content"]})
    if not msgs:
        msgs = [{"role": "user", "content": str(question)}]
    return msgs


def _problem_tools(problem: dict[str, Any]) -> list[ToolDef]:
    """Extract function specs from a BFCL problem and convert to ToolDefs."""
    funcs = problem.get("function") or []
    if not isinstance(funcs, list):
        funcs = [funcs]
    return [bfcl_func_spec_to_tool_def(f) for f in funcs]


@dataclass
class BfclInvocationResult:
    problem_id: str
    actual_calls: list[tuple[str, dict[str, Any]]]
    wall_s: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""


def run_problem_raw(
    backend: Backend,
    problem: dict[str, Any],
    *,
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> BfclInvocationResult:
    """Raw mode: single chat call, capture emitted tool calls."""
    pid = problem.get("id", "unknown")
    messages = _problem_messages(problem)
    tools = _problem_tools(problem)
    openai_tools = [t.to_openai() for t in tools] if tools else None

    t0 = time.monotonic()
    try:
        resp: ChatResponse = backend.chat(
            messages=messages,
            tools=openai_tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as e:  # noqa: BLE001 — surface backend errors per problem
        return BfclInvocationResult(
            problem_id=pid,
            actual_calls=[],
            wall_s=time.monotonic() - t0,
            error=f"{type(e).__name__}: {e}",
        )

    calls = [(tc.name, tc.arguments) for tc in resp.tool_calls]
    return BfclInvocationResult(
        problem_id=pid,
        actual_calls=calls,
        wall_s=time.monotonic() - t0,
        prompt_tokens=resp.timing.prompt_tokens,
        completion_tokens=resp.timing.completion_tokens,
    )


def run_problem_agent(
    backend: Backend,
    role_cfg: RoleConfig,
    problem: dict[str, Any],
    *,
    system_prompt: str = "You are an assistant that calls tools to answer questions.",
) -> BfclInvocationResult:
    """Agent mode: full run_agent() loop with the BFCL spec as the only
    ToolDef and stub executor. Captures all tool calls from the loop.
    """
    from luxe.agents.loop import run_agent

    pid = problem.get("id", "unknown")
    messages_seed = _problem_messages(problem)
    user_text = "\n\n".join(m["content"] for m in messages_seed if m.get("role") == "user")
    tool_defs = _problem_tools(problem)
    tool_fns = {td.name: make_stub_executor({"name": td.name}) for td in tool_defs}

    t0 = time.monotonic()
    try:
        result = run_agent(
            backend=backend,
            role_cfg=role_cfg,
            system_prompt=system_prompt,
            task_prompt=user_text,
            tool_defs=tool_defs,
            tool_fns=tool_fns,
        )
    except Exception as e:  # noqa: BLE001
        return BfclInvocationResult(
            problem_id=pid,
            actual_calls=[],
            wall_s=time.monotonic() - t0,
            error=f"{type(e).__name__}: {e}",
        )

    calls = [(tc.name, tc.arguments) for tc in result.tool_calls
             if not tc.duplicate and not tc.error]
    return BfclInvocationResult(
        problem_id=pid,
        actual_calls=calls,
        wall_s=result.wall_s or (time.monotonic() - t0),
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
