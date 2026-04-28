"""Shared agent loop — tool dispatch, schema validation, telemetry.

Mirrors luxe's agents/base.py run_agent() pattern: chat → parse tool calls →
validate → dispatch → append results → repeat until done or budget exhausted.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from luxe.backend import Backend, ChatResponse, ToolCallResponse
from luxe.config import RoleConfig
from luxe.context import context_pressure, elide_old_tool_results
from luxe.tools.base import ToolCache, ToolDef, ToolCall, ToolFn, dispatch_tool, validate_args


@dataclass
class AgentResult:
    final_text: str = ""
    steps: int = 0
    tool_calls_total: int = 0
    schema_rejects: int = 0
    aborted: bool = False
    abort_reason: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    wall_s: float = 0.0
    peak_context_pressure: float = 0.0


OnToolEvent = Callable[[ToolCall], None]


def _parse_text_tool_calls(
    text: str,
    known_names: set[str],
) -> list[ToolCallResponse]:
    """Recover tool calls from text when model doesn't use structured output."""
    calls: list[ToolCallResponse] = []

    # Qwen/Hermes: <tool_call>{"name":...,"arguments":...}</tool_call>
    for m in re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.DOTALL):
        try:
            obj = json.loads(m.group(1))
            name = obj.get("name", "")
            args = obj.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
            if name in known_names:
                calls.append(ToolCallResponse(id="", name=name, arguments=args))
                return calls  # first only
        except (json.JSONDecodeError, KeyError):
            continue

    # Bare JSON: {"name": "...", "arguments": {...}}
    for m in re.finditer(r'\{\s*"name"\s*:\s*"(\w+)".*?\}', text, re.DOTALL):
        try:
            # Try to parse the full match as JSON
            start = m.start()
            depth = 0
            end = start
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            obj = json.loads(text[start:end])
            name = obj.get("name", "")
            args = obj.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
            if name in known_names:
                calls.append(ToolCallResponse(id="", name=name, arguments=args))
                return calls
        except (json.JSONDecodeError, KeyError):
            continue

    return calls


_MAX_CONSECUTIVE_REPEAT_STEPS = 2


def _call_key(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True)}"


def run_agent(
    backend: Backend,
    role_cfg: RoleConfig,
    *,
    system_prompt: str,
    task_prompt: str,
    tool_defs: list[ToolDef],
    tool_fns: dict[str, ToolFn],
    cache: ToolCache | None = None,
    cacheable: set[str] | None = None,
    on_tool_event: OnToolEvent | None = None,
) -> AgentResult:
    """Run the agent loop: chat → tool calls → dispatch → repeat."""

    result = AgentResult()
    t0 = time.monotonic()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_prompt},
    ]

    openai_tools = [td.to_openai() for td in tool_defs] if tool_defs else None
    tool_def_map = {td.name: td for td in tool_defs}
    known_names = set(tool_def_map.keys())

    seen_calls: set[str] = set()
    consecutive_repeat_steps = 0

    for step in range(role_cfg.max_steps):
        result.steps = step + 1

        pressure = context_pressure(messages, role_cfg.num_ctx)
        result.peak_context_pressure = max(result.peak_context_pressure, pressure)

        messages = elide_old_tool_results(messages, role_cfg.num_ctx)

        try:
            resp: ChatResponse = backend.chat(
                messages,
                tools=openai_tools,
                max_tokens=role_cfg.max_tokens_per_turn,
                temperature=role_cfg.temperature,
                num_ctx=role_cfg.num_ctx,
            )
        except Exception as e:
            result.aborted = True
            result.abort_reason = f"Backend error: {e}"
            break

        result.prompt_tokens += resp.timing.prompt_tokens
        result.completion_tokens += resp.timing.completion_tokens

        tool_calls = resp.tool_calls
        if not tool_calls and resp.text and tool_defs:
            tool_calls = _parse_text_tool_calls(resp.text, known_names)

        if not tool_calls:
            result.final_text = resp.text
            break

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": resp.text or ""}
        if resp.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id or f"call_{step}_{i}",
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for i, tc in enumerate(resp.tool_calls)
            ]
        messages.append(assistant_msg)

        step_had_repeat = False
        for tc in tool_calls:
            result.tool_calls_total += 1

            if tc.name in tool_def_map:
                err = validate_args(tool_def_map[tc.name], tc.arguments)
                if err:
                    result.schema_rejects += 1
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id or f"call_{step}",
                        "name": tc.name,
                        "content": f"Schema error: {err}",
                    })
                    continue

            key = _call_key(tc.name, tc.arguments)
            if key in seen_calls:
                step_had_repeat = True
                content = (
                    f"You already called {tc.name} with these exact arguments "
                    "and the result was provided above. "
                    "Use a different tool, try different arguments, "
                    "or summarize your findings."
                )
                dup = ToolCall(
                    id=tc.id or f"call_{step}",
                    name=tc.name,
                    arguments=tc.arguments,
                    result=content,
                    cached=True,
                    duplicate=True,
                    bytes_out=0,
                    wall_s=0.0,
                )
                result.tool_calls.append(dup)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id or f"call_{step}",
                    "name": tc.name,
                    "content": content,
                })
                if on_tool_event:
                    on_tool_event(dup)
                continue

            executed = dispatch_tool(
                tc.name, tc.arguments, tool_fns,
                cache=cache, cacheable=cacheable,
            )
            result.tool_calls.append(executed)
            seen_calls.add(key)

            content = executed.error or executed.result
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id or f"call_{step}",
                "name": tc.name,
                "content": content,
            })

            if on_tool_event:
                on_tool_event(executed)

        if step_had_repeat:
            consecutive_repeat_steps += 1
            if consecutive_repeat_steps >= _MAX_CONSECUTIVE_REPEAT_STEPS:
                result.final_text = resp.text or ""
                result.aborted = True
                result.abort_reason = (
                    f"Stuck in loop — repeated same tool calls "
                    f"{consecutive_repeat_steps} consecutive turns"
                )
                break
        else:
            consecutive_repeat_steps = 0
    else:
        result.final_text = resp.text if 'resp' in dir() else ""
        result.aborted = True
        result.abort_reason = f"Max steps reached ({role_cfg.max_steps})"

    result.wall_s = time.monotonic() - t0
    return result
