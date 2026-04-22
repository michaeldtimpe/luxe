"""Shared agent loop for specialists.

Adapted from personal_eval/agent_loop.py. Simplified:
- No RunMetrics (we log to Session instead)
- Tool surface and system prompt come from AgentConfig
- Tool dispatch delegated to luxe.tools (set via the tools_registry arg)
- Streaming text is rendered by the caller (REPL); we return the final text

This is the base loop every specialist (general/research/writing/image/code)
extends. Specialists mostly just pick their tool set.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from harness.backends import Backend, ToolCall, ToolDef

from luxe.registry import AgentConfig
from luxe.session import Session

ToolFn = Callable[[dict[str, Any]], tuple[Any, str | None]]

_TOOL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_JSON_BLOCK_RE = re.compile(r"```(?:json|tool_call)?\s*(\{.*?\})\s*```", re.DOTALL)


def _safe_json(s: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _parse_text_tool_calls(text: str, known_names: set[str]) -> list[ToolCall]:
    """Recover tool calls that a model emitted as text content (Qwen/Hermes
    pattern) instead of structured tool_calls. Supports <tool_call>{...}</>
    tags, fenced ```json blocks, and standalone JSON lines."""
    candidates: list[dict[str, Any]] = []
    for m in _TOOL_TAG_RE.finditer(text):
        obj = _safe_json(m.group(1))
        if obj:
            candidates.append(obj)
    if not candidates:
        for m in _JSON_BLOCK_RE.finditer(text):
            obj = _safe_json(m.group(1))
            if obj:
                candidates.append(obj)
    if not candidates:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                obj = _safe_json(stripped)
                if obj and obj.get("name"):
                    candidates.append(obj)

    calls: list[ToolCall] = []
    for i, obj in enumerate(candidates):
        name = obj.get("name")
        if name not in known_names:
            continue
        args = obj.get("arguments") or obj.get("parameters") or {}
        if isinstance(args, str):
            args_obj = _safe_json(args) or {}
            raw = args
        else:
            args_obj = args
            raw = json.dumps(args)
        calls.append(
            ToolCall(id=f"text_{i}", name=name, arguments=args_obj, raw_arguments=raw)
        )
    return calls


@dataclass
class AgentResult:
    final_text: str
    steps_taken: int
    tool_calls_total: int
    aborted: bool = False
    abort_reason: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    wall_s: float = 0.0


def run_agent(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    tool_defs: list[ToolDef],
    tool_fns: dict[str, ToolFn],
    session: Session | None = None,
    history: list[dict[str, Any]] | None = None,
) -> AgentResult:
    """Run a specialist agent until it stops calling tools or hits a budget.

    Args:
        backend: OpenAI-compat Backend pointed at Ollama
        cfg: agent config (model, prompt, budgets)
        task: user's task description (already cleaned up by router)
        tool_defs: ToolDef specs the model sees
        tool_fns: name -> callable mapping; each returns (result, error or None)
        session: optional Session to log every turn into
        history: optional prior messages to prepend (for session resume)
    """

    messages: list[dict[str, Any]] = []
    if history:
        messages.extend(history)
    messages.append({"role": "system", "content": cfg.system_prompt})
    messages.append({"role": "user", "content": task})

    if session:
        session.append({"role": "user", "agent": cfg.name, "content": task})

    started = time.monotonic()
    tool_calls_total = 0
    step = 0
    final_text = ""
    prompt_tokens = 0
    completion_tokens = 0

    def _result(aborted: bool = False, reason: str = "") -> AgentResult:
        return AgentResult(
            final_text=final_text,
            steps_taken=step,
            tool_calls_total=tool_calls_total,
            aborted=aborted,
            abort_reason=reason,
            transcript=messages,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            wall_s=time.monotonic() - started,
        )

    while step < cfg.max_steps:
        if time.monotonic() - started > cfg.max_wall_s:
            return _result(True, f"wall budget {cfg.max_wall_s}s exceeded")

        try:
            response = backend.chat(
                messages,
                tools=tool_defs or None,
                max_tokens=cfg.max_tokens_per_turn,
                temperature=cfg.temperature,
                stream=False,  # REPL does its own streaming layer in a later phase
            )
        except KeyboardInterrupt:
            return _result(True, "interrupted (Ctrl-C)")
        prompt_tokens += response.timing.prompt_tokens
        completion_tokens += response.timing.completion_tokens

        # Ollama emits most qwen/hermes tool calls in `tool_calls` already,
        # but qwen2.5-coder sometimes falls back to text JSON. Recover only
        # the first call — models that dump a whole speculative plan would
        # otherwise blow past the per-turn cap in a single step.
        if not response.tool_calls and response.text and tool_defs:
            known = {t.name for t in tool_defs}
            recovered = _parse_text_tool_calls(response.text, known)
            if recovered:
                response.tool_calls = recovered[:1]
                response.text = ""

        final_text = response.text or final_text
        tool_calls_total += len(response.tool_calls)

        if len(response.tool_calls) > cfg.max_tool_calls_per_turn:
            return _result(
                True,
                f"runaway turn: {len(response.tool_calls)} tool calls "
                f"> cap {cfg.max_tool_calls_per_turn}",
            )

        if not response.tool_calls:
            # Final answer
            final_text = response.text
            step += 1
            if response.text:
                messages.append({"role": "assistant", "content": response.text})
                if session:
                    session.append(
                        {"role": "assistant", "agent": cfg.name, "content": response.text}
                    )
            return _result()

        # Model wants tools. Append assistant turn with tool_calls, then each result.
        messages.append(
            {
                "role": "assistant",
                "content": response.text or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.raw_arguments},
                    }
                    for tc in response.tool_calls
                ],
            }
        )

        for call in response.tool_calls:
            fn = tool_fns.get(call.name)
            if not fn:
                result: Any = None
                err: str | None = f"unknown tool: {call.name}"
            else:
                try:
                    result, err = fn(call.arguments)
                except Exception as e:  # noqa: BLE001
                    result, err = None, f"{type(e).__name__}: {e}"

            tool_content = _trim(result if err is None else f"ERROR: {err}")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": tool_content,
                }
            )
            if session:
                session.append(
                    {
                        "role": "tool",
                        "agent": cfg.name,
                        "tool": call.name,
                        "arguments": call.arguments,
                        "result": tool_content,
                        "error": err,
                    }
                )

        step += 1

    return _result(True, f"step budget {cfg.max_steps} exhausted")


def _trim(value: Any, limit: int = 4000) -> str:
    s = value if isinstance(value, str) else str(value)
    return s if len(s) <= limit else s[:limit] + f"\n... [truncated {len(s) - limit} bytes]"
