"""Token estimation and context pressure monitoring."""

from __future__ import annotations

import json
from typing import Any


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                total += estimate_tokens(str(part))
        if "tool_calls" in msg:
            total += estimate_tokens(json.dumps(msg["tool_calls"]))
        total += 4  # message framing overhead
    return total


def context_pressure(messages: list[dict[str, Any]], ctx_limit: int) -> float:
    if ctx_limit <= 0:
        return 0.0
    return estimate_messages_tokens(messages) / ctx_limit


def elide_old_tool_results(
    messages: list[dict[str, Any]],
    ctx_limit: int,
    threshold: float = 0.7,
    keep_recent: int = 4,
) -> list[dict[str, Any]]:
    """Replace old tool results with stubs when pressure exceeds threshold."""
    if context_pressure(messages, ctx_limit) < threshold:
        return messages

    tool_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "tool"
    ]
    if len(tool_indices) <= keep_recent:
        return messages

    elide_set = set(tool_indices[:-keep_recent])
    result = []
    for i, msg in enumerate(messages):
        if i in elide_set:
            content = msg.get("content", "")
            size = len(content.encode("utf-8", errors="replace"))
            name = msg.get("name", "tool")
            stub = f"[elided: {name} -> {size} bytes]"
            result.append({**msg, "content": stub})
        else:
            result.append(msg)
    return result
