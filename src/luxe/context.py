"""Token estimation, context pressure monitoring, and compaction strategies.

Forge-hybrid Phase 2 (A) adds `TieredCompact` — a 3-phase context compaction
strategy ported from forge.context.strategies. Gated behind `LUXE_TIERED_COMPACT=1`
(default OFF, byte-identical baseline). The existing `elide_old_tool_results`
remains the default fallback.

Plan: ~/.claude/plans/starry-hopping-phoenix.md
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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


# ── TieredCompact (forge-hybrid Phase 2 A) ──────────────────────────────


def _is_nudge(msg: dict[str, Any]) -> bool:
    """A message is a nudge if any guardrail or repair tagged it.

    Reads:
    - `_luxe_nudge` (forge-hybrid Phase 1 C marker; tagged by guards in loop.py)
    - `_luxe_repair` (BFCL Phase 2 reflect/repair marker; tagged by reflect.py)

    See docs/luxe-markers-audit.md for the full classification.
    """
    return bool(msg.get("_luxe_nudge")) or bool(msg.get("_luxe_repair"))


def _is_tool_result(msg: dict[str, Any]) -> bool:
    return msg.get("role") == "tool"


def _is_text_response(msg: dict[str, Any]) -> bool:
    """Assistant message with text content but no tool_calls."""
    if msg.get("role") != "assistant":
        return False
    return not msg.get("tool_calls")


def _is_tool_call(msg: dict[str, Any]) -> bool:
    """Assistant message that issued tool_calls (content may be the reasoning preamble)."""
    return msg.get("role") == "assistant" and bool(msg.get("tool_calls"))


@dataclass(frozen=True)
class CompactionResult:
    """Telemetry payload from a TieredCompact.compact() call.

    Attributes:
        messages: The compacted (or unchanged) message list.
        phase_reached: 0 = no compaction; 1 = nudges dropped + tool_results truncated;
            2 = + tool_results dropped entirely; 3 = + text/reasoning dropped.
        tokens_before: Estimated token count before compaction.
        tokens_after: Estimated token count after compaction.
        tool_results_dropped: Count of tool_result messages truncated or dropped
            (Phase 1 truncations and Phase 2 drops both contribute).
    """

    messages: list[dict[str, Any]]
    phase_reached: int
    tokens_before: int
    tokens_after: int
    tool_results_dropped: int


class TieredCompact:
    """Three-phase compaction strategy (ported from forge.context.strategies).

    Phase priority (cut first -> preserve longest):
      Phase 1: drop _luxe_nudge / _luxe_repair messages + truncate tool_results
               to TRUNCATE_CHARS chars (with a "[Truncated]" suffix).
      Phase 2: + drop tool_results entirely.
      Phase 3: + drop text-response assistant messages (no tool_calls);
               clear `content` on tool_call assistant messages (skeleton only).

    Each phase runs only if the previous phase didn't reduce tokens below the
    compact_threshold. messages[0:2] (system prompt + original task) are NEVER
    dropped. The last `keep_recent` assistant-message iterations are protected
    too — only messages between [2, eligible_end) are eligible for compaction.

    Defaults: keep_recent=3 (matches the forge-hybrid plan; deeper than forge's
    own keep_recent=2 because SWE-bench trajectories run 12-30 steps),
    compact_threshold=0.75.

    See `docs/luxe-markers-audit.md` for the nudge-marker classification.
    """

    TRUNCATE_CHARS = 200

    def __init__(
        self,
        keep_recent: int = 3,
        compact_threshold: float = 0.75,
    ) -> None:
        self.keep_recent = keep_recent
        self.compact_threshold = compact_threshold

    @staticmethod
    def _find_eligible_end(messages: list[dict[str, Any]], keep_recent: int) -> int:
        """Return the boundary index: messages before this are eligible.

        Each `role == "assistant"` message starts a new loop iteration.
        Walking from the end backwards, count assistant boundaries until
        keep_recent are passed; that assistant's index is the eligible_end.

        If fewer than keep_recent assistant messages exist, return 2
        (nothing eligible — protect the whole thing).
        """
        count = 0
        for i in range(len(messages) - 1, 1, -1):
            if messages[i].get("role") == "assistant":
                count += 1
                if count == keep_recent:
                    return i
        return 2

    def compact(
        self,
        messages: list[dict[str, Any]],
        ctx_limit: int,
    ) -> CompactionResult:
        """Apply tiered compaction. Returns the (possibly unchanged) messages + telemetry."""
        tokens_before = estimate_messages_tokens(messages)
        if ctx_limit <= 0:
            return CompactionResult(
                messages=list(messages),
                phase_reached=0,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                tool_results_dropped=0,
            )
        trigger = int(ctx_limit * self.compact_threshold)
        if tokens_before < trigger:
            return CompactionResult(
                messages=list(messages),
                phase_reached=0,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                tool_results_dropped=0,
            )

        eligible_end = self._find_eligible_end(messages, self.keep_recent)

        result, dropped = self._phase1(messages, eligible_end)
        tokens_after = estimate_messages_tokens(result)
        if tokens_after < trigger:
            return CompactionResult(
                messages=result,
                phase_reached=1,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                tool_results_dropped=dropped,
            )

        result, dropped = self._phase2(messages, eligible_end)
        tokens_after = estimate_messages_tokens(result)
        if tokens_after < trigger:
            return CompactionResult(
                messages=result,
                phase_reached=2,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                tool_results_dropped=dropped,
            )

        result, dropped = self._phase3(messages, eligible_end)
        tokens_after = estimate_messages_tokens(result)
        return CompactionResult(
            messages=result,
            phase_reached=3,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tool_results_dropped=dropped,
        )

    def _phase1(
        self,
        messages: list[dict[str, Any]],
        eligible_end: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Drop nudges + truncate tool_results outside keep_recent."""
        result: list[dict[str, Any]] = []
        dropped = 0
        for i, msg in enumerate(messages):
            if 2 <= i < eligible_end:
                if _is_nudge(msg):
                    continue
                if _is_tool_result(msg):
                    content = msg.get("content", "") or ""
                    if len(content) > self.TRUNCATE_CHARS:
                        kept = content[: self.TRUNCATE_CHARS]
                        removed = len(content) - self.TRUNCATE_CHARS
                        result.append({
                            **msg,
                            "content": f"{kept}\n[Truncated — {removed} chars removed]",
                        })
                        dropped += 1
                        continue
            result.append(msg)
        return result, dropped

    def _phase2(
        self,
        messages: list[dict[str, Any]],
        eligible_end: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Phase 1 + drop tool_results entirely."""
        result: list[dict[str, Any]] = []
        dropped = 0
        for i, msg in enumerate(messages):
            if 2 <= i < eligible_end:
                if _is_nudge(msg):
                    continue
                if _is_tool_result(msg):
                    dropped += 1
                    continue
            result.append(msg)
        return result, dropped

    def _phase3(
        self,
        messages: list[dict[str, Any]],
        eligible_end: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Phase 2 + drop text-response messages; clear content on tool_call messages."""
        result: list[dict[str, Any]] = []
        dropped = 0
        for i, msg in enumerate(messages):
            if 2 <= i < eligible_end:
                if _is_nudge(msg):
                    continue
                if _is_tool_result(msg):
                    dropped += 1
                    continue
                if _is_text_response(msg):
                    continue
                if _is_tool_call(msg):
                    result.append({**msg, "content": ""})
                    continue
            result.append(msg)
        return result, dropped
