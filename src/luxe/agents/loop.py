"""Shared agent loop — tool dispatch, schema validation, telemetry.

Mirrors luxe's agents/base.py run_agent() pattern: chat → parse tool calls →
validate → dispatch → append results → repeat until done or budget exhausted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from luxe.backend import Backend, ChatResponse, ToolCallResponse
from luxe.config import RoleConfig
from luxe.context import context_pressure, elide_old_tool_results
from luxe.run_state import append_event
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

# Tools exempt from duplicate-call detection. Reads are idempotent in name
# but post-write semantics differ — re-reading after an edit returns the
# updated content, which the model relies on to verify edits landed.
# Deduplicating reads strands the model: it tries to verify a write,
# gets "you already called this" instead of fresh content, panics,
# and retries the write — which then trips the streak-abort. Only
# write/search tools where re-running yields no new information stay
# in the dedup path.
_DEDUP_EXEMPT_TOOLS = {"read_file"}

# Tools considered "write" actions for mid-loop write-pressure detection.
# Tasks that produce a deliverable diff must hit at least one of these — a
# loop that reads many times without ever writing is the prose-mode trap
# observed on nothing-ever-happens-document-config (v1.4.0 rep 1: 17 tool
# calls, 9092 completion tokens, 0 writes; model declared "comprehensive
# picture" prematurely, hallucinated content from priors, never committed).
_WRITE_TOOLS = frozenset({"write_file", "edit_file"})

# Mid-loop write-pressure thresholds (Mode B fix). Fires once per run when
# all conditions hold: tool calls >= MIN_TOOLS, completion tokens >=
# MIN_TOKENS, current step >= MIN_STEP, zero writes so far. Off by default;
# enable per-run with LUXE_WRITE_PRESSURE=1 (or via runtime config).
_WRITE_PRESSURE_MIN_TOOLS = 10
_WRITE_PRESSURE_MIN_TOKENS = 4000
_WRITE_PRESSURE_MIN_STEP = 5

_WRITE_PRESSURE_MESSAGE = (
    "Mid-loop notice: you've issued multiple reads without writing or "
    "editing any files. This task's deliverable is a concrete diff — "
    "re-reading existing material cannot produce one. Stop reading and "
    "call `write_file` or `edit_file` now with the deliverable based on "
    "what you've already learned. If specific details are missing, write "
    "a first draft that captures the structure, then refine."
)

# Emit a progress line each time cumulative completion tokens crosses a
# multiple of this threshold. Useful for spotting bailout vs full-engagement
# patterns mid-run. Set to 0 to disable. Configurable via env.
import os as _os_for_logging
_TOKEN_LOG_INTERVAL = int(_os_for_logging.environ.get("LUXE_TOKEN_LOG_INTERVAL", "5000"))


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
    run_id: str | None = None,
    phase: str = "main",
) -> AgentResult:
    """Run the agent loop: chat → tool calls → dispatch → repeat."""

    result = AgentResult()
    t0 = time.monotonic()
    log_calls = bool(run_id) and os.environ.get("LUXE_LOG_TOOL_CALLS") == "1"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_prompt},
    ]

    openai_tools = [td.to_openai() for td in tool_defs] if tool_defs else None
    tool_def_map = {td.name: td for td in tool_defs}
    known_names = set(tool_def_map.keys())

    seen_calls: set[str] = set()
    consecutive_repeat_steps = 0
    next_token_log_threshold = _TOKEN_LOG_INTERVAL  # 0 = disabled
    write_pressure_enabled = os.environ.get("LUXE_WRITE_PRESSURE") == "1"
    write_pressure_fired = False
    writes_seen = 0

    for step in range(role_cfg.max_steps):
        result.steps = step + 1

        pressure = context_pressure(messages, role_cfg.num_ctx)
        result.peak_context_pressure = max(result.peak_context_pressure, pressure)

        # Mid-loop write-pressure injection (Mode B fix). Fires once per
        # run when the agent has done substantial reading + generation
        # without writing. Targets the prose-mode trap where the model
        # declares "comprehensive picture" prematurely and hallucinates
        # the deliverable into chat instead of committing it. The
        # synthetic user message interrupts the read-loop and forces a
        # write decision before further tool calls accumulate.
        if (write_pressure_enabled
                and not write_pressure_fired
                and writes_seen == 0
                and step >= _WRITE_PRESSURE_MIN_STEP
                and result.tool_calls_total >= _WRITE_PRESSURE_MIN_TOOLS
                and result.completion_tokens >= _WRITE_PRESSURE_MIN_TOKENS):
            messages.append({"role": "user", "content": _WRITE_PRESSURE_MESSAGE})
            write_pressure_fired = True
            if log_calls:
                append_event(
                    run_id, "write_pressure_fired",
                    phase=phase, step=step,
                    tool_calls_total=result.tool_calls_total,
                    completion_tokens=result.completion_tokens,
                )

        messages = elide_old_tool_results(messages, role_cfg.num_ctx)

        try:
            resp: ChatResponse = backend.chat(
                messages,
                tools=openai_tools,
                max_tokens=role_cfg.max_tokens_per_turn,
                temperature=role_cfg.temperature,
                num_ctx=role_cfg.num_ctx,
                repeat_penalty=role_cfg.repeat_penalty,
            )
        except Exception as e:
            result.aborted = True
            result.abort_reason = f"Backend error: {e}"
            break

        result.prompt_tokens += resp.timing.prompt_tokens
        result.completion_tokens += resp.timing.completion_tokens

        # Token-interval progress logging — fires when cumulative completion
        # tokens crosses each LUXE_TOKEN_LOG_INTERVAL multiple. Lets us see
        # whether a model is steadily generating with tool calls (engaged)
        # vs bursting prose without tools (bailing).
        if (next_token_log_threshold > 0
                and result.completion_tokens >= next_token_log_threshold):
            print(
                f"    [token-progress] step={step+1} "
                f"completion_tokens={result.completion_tokens} "
                f"prompt_tokens={result.prompt_tokens} "
                f"tool_calls={result.tool_calls_total} "
                f"ctx_pressure={pressure:.0%}",
                flush=True,
            )
            while next_token_log_threshold <= result.completion_tokens:
                next_token_log_threshold += _TOKEN_LOG_INTERVAL

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
            key_hash = hashlib.sha1(key.encode()).hexdigest()[:8]
            if key in seen_calls and tc.name not in _DEDUP_EXEMPT_TOOLS:
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
                if log_calls:
                    append_event(
                        run_id, "tool_call",
                        phase=phase, step=step, name=tc.name,
                        key_hash=key_hash, duplicate=True, cached=False,
                        bytes_out=0,
                    )
                continue

            executed = dispatch_tool(
                tc.name, tc.arguments, tool_fns,
                cache=cache, cacheable=cacheable,
            )
            result.tool_calls.append(executed)
            seen_calls.add(key)
            if tc.name in _WRITE_TOOLS and not executed.error:
                writes_seen += 1

            content = executed.error or executed.result
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id or f"call_{step}",
                "name": tc.name,
                "content": content,
            })

            if on_tool_event:
                on_tool_event(executed)
            if log_calls:
                append_event(
                    run_id, "tool_call",
                    phase=phase, step=step, name=tc.name,
                    key_hash=key_hash, duplicate=False,
                    cached=executed.cached, bytes_out=executed.bytes_out,
                )

        if step_had_repeat:
            consecutive_repeat_steps += 1
            if log_calls:
                append_event(
                    run_id, "tool_step_done",
                    phase=phase, step=step,
                    step_had_repeat=True,
                    consecutive_repeat_steps=consecutive_repeat_steps,
                )
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
            if log_calls:
                append_event(
                    run_id, "tool_step_done",
                    phase=phase, step=step,
                    step_had_repeat=False,
                    consecutive_repeat_steps=0,
                )
    else:
        result.final_text = resp.text if 'resp' in dir() else ""
        result.aborted = True
        result.abort_reason = f"Max steps reached ({role_cfg.max_steps})"

    result.wall_s = time.monotonic() - t0
    return result
