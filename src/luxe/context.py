"""Token estimation, context pressure monitoring, and compaction strategies.

Forge-hybrid Phase 2 (A) adds `TieredCompact` — a 3-phase context compaction
strategy ported from forge.context.strategies. Gated behind `LUXE_TIERED_COMPACT=1`
(default OFF, byte-identical baseline). The existing `elide_old_tool_results`
remains the default fallback.

Plan: ~/.claude/plans/starry-hopping-phoenix.md
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PinnedContext:
    goal: str = ""
    active_phase: str = ""
    completed_steps: list[str] = field(default_factory=list)
    known_findings: list[str] = field(default_factory=list)
    current_blocker: str | None = None
    next_action: str = ""
    verified_findings: list[str] = field(default_factory=list)

    def add_finding(self, finding: str) -> None:
        if finding in self.known_findings:
            self.known_findings.remove(finding)
        self.known_findings.append(finding)
        if len(self.known_findings) > 50:
            self.known_findings.pop(0)

    def to_markdown(self) -> str:
        lines = [
            "### AGENT STATE (Pinned Context)",
            f"- **Goal**: {self.goal}",
            f"- **Active Phase**: {self.active_phase}",
        ]
        if self.next_action:
            lines.append(f"- **Next Action**: {self.next_action}")
        if self.current_blocker:
            lines.append(f"- **Current Blocker**: {self.current_blocker}")
        if self.completed_steps:
            lines.append("- **Completed Steps**:")
            for step in self.completed_steps:
                lines.append(f"  - {step}")
        if self.known_findings:
            lines.append("- **Known Findings**:")
            for finding in self.known_findings:
                lines.append(f"  - {finding}")
        if self.verified_findings:
            lines.append("- **Verified Findings**:")
            for finding in self.verified_findings:
                lines.append(f"  - {finding}")
        return "\n".join(lines)


def _is_pinned_context(msg: dict[str, Any]) -> bool:
    return bool(msg.get("_luxe_pinned_context")) or msg.get("name") == "pinned_context"



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

    Per-phase thresholds (phase_thresholds): a (phase1, phase2, phase3) tuple
    of trigger fractions. When set, each phase fires at its own threshold.
    The forge-hybrid Phase 2 (A) n=75 4-arm sweep showed phase 1 fires HEAL
    protected wrong_target instances while phase 3 fires DESTROY existing
    patches — so the right tuning is aggressive phase 1 + conservative phase 3
    (e.g., (0.50, 0.85, 0.95)). When phase_thresholds is None, falls back to
    compact_threshold for all 3 phases (backwards compat).

    See `docs/luxe-markers-audit.md` for the nudge-marker classification.
    """

    TRUNCATE_CHARS = 200

    # Default phase_thresholds (0.50, 0.85, 0.95) — shipped 2026-05-28 as the
    # forge-hybrid cycle's only Pareto-positive default. n=75 rep-1+rep-2
    # validation: resolve-rate equivalent to baseline (60/75 then 58/75 vs
    # baseline 58/75, within substrate noise ±2.8) AND 42-56% wall savings
    # AND 2 protected wrong_target instances healed (matplotlib-25775,
    # pylint-6528) AND zero new wrong_target damages. Aggressive phase 1
    # (fire at 50% pressure) captures recovery wins; conservative phase 3
    # (fire at 95% pressure, observed 1/75 firing rate) avoids the
    # destructive reasoning-drop mode. See lessons.md 2026-05-28 entry.
    _DEFAULT_PHASE_THRESHOLDS: tuple[float, float, float] = (0.50, 0.85, 0.95)

    def __init__(
        self,
        keep_recent: int = 3,
        compact_threshold: float = 0.75,
        phase_thresholds: tuple[float, float, float] | None = None,
    ) -> None:
        self.keep_recent = keep_recent
        self.compact_threshold = compact_threshold
        if phase_thresholds is not None:
            self._phase_triggers = phase_thresholds
        else:
            self._phase_triggers = self._DEFAULT_PHASE_THRESHOLDS

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
        backend: Any = None,
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
        t1 = int(ctx_limit * self._phase_triggers[0])
        t2 = int(ctx_limit * self._phase_triggers[1])
        t3 = int(ctx_limit * self._phase_triggers[2])
        if tokens_before < t1:
            return CompactionResult(
                messages=list(messages),
                phase_reached=0,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                tool_results_dropped=0,
            )

        eligible_end = self._find_eligible_end(messages, self.keep_recent)

        # Opt-in Rich Semantic Compaction
        import os
        if os.environ.get("LUXE_RICH_COMPACT") == "1" and backend is not None:
            eligible_messages = messages[2:eligible_end]
            text_to_summarize = []
            for msg in eligible_messages:
                if _is_pinned_context(msg) or _is_nudge(msg):
                    continue
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    text_to_summarize.append(f"{role.upper()}: {content}")
                elif isinstance(content, list):
                    text_to_summarize.append(f"{role.upper()}: {str(content)}")

            if text_to_summarize:
                summary_prompt = [
                    {
                        "role": "system",
                        "content": "You are a highly efficient assistant. Summarize the following agent conversation history and tool outputs extremely concisely. Focus only on key findings, changes made, and files analyzed. Avoid wordy explanations."
                    },
                    {
                        "role": "user",
                        "content": "\n".join(text_to_summarize)
                    }
                ]

                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(backend.chat, summary_prompt, max_tokens=300, temperature=0.2)
                    try:
                        resp = future.result(timeout=8.0)
                        summary_text = resp.text

                        # Construct compacted list
                        compacted_eligible = []
                        compacted_eligible.append({
                            "role": "system",
                            "content": f"[Conversation history prior to this point summarized for context: {summary_text}]",
                            "_luxe_pinned_context": True
                        })
                        for msg in eligible_messages:
                            if _is_pinned_context(msg) or _is_nudge(msg):
                                compacted_eligible.append(msg)

                        result = messages[0:2] + compacted_eligible + messages[eligible_end:]
                        tokens_after = estimate_messages_tokens(result)
                        return CompactionResult(
                            messages=result,
                            phase_reached=3,
                            tokens_before=tokens_before,
                            tokens_after=tokens_after,
                            tool_results_dropped=len(eligible_messages),
                        )
                    except concurrent.futures.TimeoutError:
                        # Timeout breached, fall back to Phase 3
                        pass

        result, dropped = self._phase1(messages, eligible_end)
        tokens_after = estimate_messages_tokens(result)
        if tokens_after < t2:
            return CompactionResult(
                messages=result,
                phase_reached=1,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                tool_results_dropped=dropped,
            )

        result, dropped = self._phase2(messages, eligible_end)
        tokens_after = estimate_messages_tokens(result)
        if tokens_after < t3:
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
                if _is_pinned_context(msg):
                    result.append(msg)
                    continue
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
                if _is_pinned_context(msg):
                    result.append(msg)
                    continue
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
                if _is_pinned_context(msg):
                    result.append(msg)
                    continue
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
