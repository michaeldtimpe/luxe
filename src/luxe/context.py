"""Token estimation, context pressure monitoring, and compaction strategies.

Forge-hybrid Phase 2 (A) adds `TieredCompact` — a 3-phase context compaction
strategy ported from forge.context.strategies. Gated behind `LUXE_TIERED_COMPACT=1`
(default OFF, byte-identical baseline). The existing `elide_old_tool_results`
remains the default fallback.

Plan: ~/.claude/plans/starry-hopping-phoenix.md
"""

from __future__ import annotations

import json
import math
import re
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


#: Bounds on the server-truth calibration ratio. `estimate_tokens` is chars//4,
#: which runs ~1.9x low on code + JSON tool payloads and would run HIGH only on
#: pathological input; the clamp keeps one bad `prompt_tokens` report (a server
#: counting cached tokens differently, a 0, a malformed usage block) from
#: swinging compaction to either extreme.
CALIBRATION_MIN = 0.5
CALIBRATION_MAX = 8.0


def calibration_ratio(actual_prompt_tokens: int, estimated_tokens: int) -> float:
    """How far `estimate_messages_tokens` undercounts, per the server's own
    `usage.prompt_tokens` for the request that estimate described.

    Returns 1.0 (no correction) when either side is missing, so a backend that
    reports no usage degrades to the historical estimate-only behaviour rather
    than to a wrong number."""
    if actual_prompt_tokens <= 0 or estimated_tokens <= 0:
        return 1.0
    ratio = actual_prompt_tokens / estimated_tokens
    return min(CALIBRATION_MAX, max(CALIBRATION_MIN, ratio))


#: Ratio assumed for prompt material the calibration never saw (2026-08-24,
#: `acceptance/chat_bigread_2026_08_24/EVIDENCE.md` finding 3). The measured
#: ratio describes ONE prompt; when the next prompt is 40x larger, all but
#: ~2% of it is material that sample never covered. The corpus says what that
#: material costs: the same sessions whose step-1 prompts (system prompt +
#: tool JSON) measured 2.64x / 2.16x / 1.91x / 1.79x recalibrated to **1.27x
#: and 1.21x** the moment a prose file landed. So 1.2 is the measured
#: chars/4-undercount of ordinary body text, cited the same way
#: `tools.fs._CHARS_PER_TOKEN` cites its own live measurement — NOT a guess
#: and NOT 1.0. Damping toward 1.0 instead would under-read prose by ~17%,
#: which is the historical (pre-2026-08-11) failure in miniature.
#:
#: It is nonetheless a FREE PARAMETER derived from one markdown-heavy session,
#: so it is overridable per run (`LUXE_CTX_CAL_UNMEASURED_RATIO`, parsed in
#: `agents/flags.py`) and the promotion bench sweeps it. A constant benched in
#: a form that cannot move is the C10 trap (lessons.md 2026-08-11).
CALIBRATION_UNMEASURED_RATIO = 1.2


def damped_calibration(
    calibration: float,
    est_at_calibration: int,
    est_now: int,
    unmeasured: float = CALIBRATION_UNMEASURED_RATIO,
) -> float:
    """Shrink a calibration toward the unmeasured-material ratio in proportion
    to how much of the CURRENT prompt the calibration sample actually covered.

    `calibration_ratio` measures one request. The loop then applies it to the
    NEXT request, which may be an order of magnitude larger and made of
    entirely different material. Real tokens are additive over characters, so
    the honest reading of a prompt whose composition shifted is a char-weighted
    blend of the two ratios::

        share    = est_at_calibration / est_now         (the measured share)
        damped   = R_unmeasured + (calibration - R_unmeasured) * share

    with `R_unmeasured = unmeasured`, defaulting to
    `CALIBRATION_UNMEASURED_RATIO`. It is a PARAMETER rather than a read of the
    module constant so the promotion bench can sweep it without editing source
    — see `RunFlags.ctx_cal_unmeasured_ratio` / `LUXE_CTX_CAL_UNMEASURED_RATIO`.
    Two properties make it safe rather than merely plausible:

    - **It only ever moves TOWARD 1.0, never past it and never away from it.**
      The blend is clamped to the interval between `calibration` and 1.0, so an
      uncalibrated step (1.0) is returned untouched, a backend reporting no
      usage still degrades to 1.0, and a ratio below 1.0 (the estimate ran
      HIGH) cannot be inflated by the unmeasured constant.
    - **The [CALIBRATION_MIN, CALIBRATION_MAX] clamp is preserved by
      construction** — the result lies between two values that are already
      inside it.

    Gradual growth is barely touched (share≈0.9 keeps ~90% of the correction),
    which is the SWE-bench/maintain shape. The 40x single-step jump that
    reported 102.5% on a request that was ~65% of the window is the shape this
    exists for.

    Live worked example (session `168f1825a1fd`, 2026-08-24): a 1.88x ratio
    measured on a ~1,650-token prompt, applied to a 71,616-token estimate,
    damps to 1.22x — 66% of a 128K window instead of 102.5%.
    """
    if est_at_calibration <= 0 or est_now <= 0:
        return calibration
    if not math.isfinite(calibration) or calibration <= 0:
        return calibration
    share = min(1.0, est_at_calibration / est_now)
    blended = unmeasured + (calibration - unmeasured) * share
    lo, hi = (1.0, calibration) if calibration >= 1.0 else (calibration, 1.0)
    return min(hi, max(lo, blended))


def calibrated_ctx_limit(ctx_limit: int, calibration: float) -> int:
    """Fold the calibration into the DENOMINATOR instead of the numerator.

    Every consumer downstream (`context_pressure`, `TieredCompact.compact`,
    `elide_old_tool_results`) computes `estimate / limit` internally. Shrinking
    the limit by the same factor the estimate runs low by makes that quotient
    equal the true `actual_tokens / ctx_limit` without any of them having to
    learn about calibration. One lever, no duplicated math, and the phase
    thresholds keep their pinned values.

    Total by construction: a non-finite or non-positive calibration returns the
    limit unchanged. `calibration_ratio`'s clamp already makes those
    unreachable from the loop, but this is a public helper and `int(nan)`
    raises — a pressure calculation must never be the thing that kills a run.
    """
    if ctx_limit <= 0 or not math.isfinite(calibration) or calibration <= 0:
        return ctx_limit
    return max(1, int(ctx_limit / calibration))


# ── single-result clamp (LUXE_TOOL_RESULT_CLAMP, 2026-08-24) ────────────
#
# `TieredCompact` cannot solve an oversized SINGLE result and must not be
# asked to. `agents.sdd` pins `messages[0]`/`messages[1]` and the last
# `keep_recent` assistant iterations as never eligible; at step 3 of a chat
# turn there are two assistant messages, `_find_eligible_end` returns 2, and
# the most aggressive phase drops nothing (`acceptance/chat_bigread_2026_08_24/
# EVIDENCE.md`: phase_reached=3, tokens_before == tokens_after == 71616,
# tool_results_dropped=0). Relaxing that invariant to reach the offending
# result would trade a load-bearing protection for a problem that belongs one
# layer down — at CREATION. So the bound lives here and is applied where the
# tool result is appended.
#
# Mostly redundant with `read_file`'s own `LUXE_TOOL_BUDGET_CTX` budget; the
# value is `bash`, `grep`, and MCP tools, which have no budget at all.

#: Share of the (calibrated) window one tool result may occupy, and the floor
#: below which the bound stops shrinking. Deliberately the same 0.25 / 8 KB
#: pair as `tools.fs.READ_BUDGET_FRACTION` / `READ_BUDGET_FLOOR` so the two
#: budgets cannot disagree about what "one result" is worth.
TOOL_RESULT_CLAMP_FRACTION = 0.25
TOOL_RESULT_CLAMP_FLOOR_CHARS = 8 * 1024

#: `read_file` numbers every line `f"{n}\t{line}"`. Matching it is how the
#: clamp recovers a resume offset that is TRUE rather than invented.
_NUMBERED_LINE_RE = re.compile(r"^(\d+)\t")


def tool_result_clamp_chars(ctx_limit: int) -> int:
    """Characters one tool result may contribute to a `ctx_limit`-token window.

    Counted in CHARACTERS, not bytes, because characters are what
    `estimate_tokens` (chars//4) divides — a character budget is the one that
    actually bounds the reading every compaction threshold keys on. Hence the
    `* 4`: it is `estimate_tokens`' own inverse, not a second tokenizer guess.

    Pass the CALIBRATED limit (`calibrated_ctx_limit`) and the bound tightens
    as the loop learns the real ratio: at cal=2.4x a 32,768-token window
    yields 13,653 chars, the same number `tools.fs.budget_for_ctx(32768)`
    reaches from the other direction. Step 1 is uncalibrated, so the first
    step's bound is the loosest one — by design; there is nothing to correct
    with yet.

    Returns 0 for a non-positive limit, which every caller reads as "no bound".
    """
    if ctx_limit <= 0:
        return 0
    return max(TOOL_RESULT_CLAMP_FLOOR_CHARS,
               int(ctx_limit * TOOL_RESULT_CLAMP_FRACTION * 4))


def clamp_tool_result(
    content: str,
    *,
    tool_name: str,
    max_chars: int,
    path: str | None = None,
) -> tuple[str, int]:
    """Bound one tool result. Returns `(content, chars_dropped)`.

    Identity (and `chars_dropped == 0`) when `max_chars <= 0` or the result
    already fits — the caller can therefore apply it unconditionally and the
    disabled path is the untouched string.

    The trailer is honest about which of two situations the model is in:

    - `read_file` output is line-numbered, so a resume offset can be RECOVERED
      from the last whole line that survived. The trailer then carries the
      same `continue with read_file(path=…, offset=N, limit=N)` shape
      `tools.fs` already emits, and the offset is real.
    - Every other tool — `bash`, `grep`, an MCP call — has no offset to offer.
      Inventing one would send the model round the identical call forever, so
      the trailer says what was dropped and that re-running will not recover
      it. Naming the loss is the point: the alternative is a silently short
      result the model treats as complete.
    """
    if max_chars <= 0 or len(content) <= max_chars:
        return content, 0

    total = len(content)
    kept = content[:max_chars]
    # Never hand back a half-written final line: it reads as data.
    nl = kept.rfind("\n")
    if nl > 0:
        kept = kept[: nl + 1]

    resume = _read_file_resume(kept, path) if tool_name == "read_file" else None
    dropped = total - len(kept)
    if resume is not None:
        offset, limit = resume
        trailer = (
            f"\n[truncated at {max_chars:,} chars — {dropped:,} of {total:,} "
            f"chars dropped before the model saw them; continue with "
            f'read_file(path="{path}", offset={offset}, limit={limit})]\n'
        )
    else:
        trailer = (
            f"\n[truncated at {max_chars:,} chars — {dropped:,} of {total:,} "
            f"chars from {tool_name} were dropped and are NOT recoverable by "
            f"re-running this call. Narrow it (a more specific pattern, a "
            f"smaller range, a `| head`) and call again.]\n"
        )
    return kept + trailer, dropped


def _read_file_resume(kept: str, path: str | None) -> tuple[int, int] | None:
    """`(offset, limit)` for the `read_file` window that continues `kept`.

    `tools.fs` numbers each line `f"{i + offset + 1}\t{line}"`, so a line
    LABELLED n is 0-based index n-1 and the next unread line is index n —
    i.e. `offset=n`. Returns None when `path` is missing or nothing in `kept`
    carries a line number (an error string, a non-`read_file` shape): no
    number recovered means no resume offered, never a guessed one.
    """
    if not path:
        return None
    lines = kept.splitlines()
    numbered = 0
    last: int | None = None
    for line in lines:
        m = _NUMBERED_LINE_RE.match(line)
        if m:
            numbered += 1
            last = int(m.group(1))
    if last is None:
        return None
    return last, max(1, numbered)


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
        eligible_end: The `[2, eligible_end)` boundary the phases operated on,
            or None when no phase ran (pressure below trigger, or ctx_limit
            <= 0). `eligible_end == 2` means NOTHING was eligible — the
            single fact that explains an ineffective fire.
    """

    messages: list[dict[str, Any]]
    phase_reached: int
    tokens_before: int
    tokens_after: int
    tool_results_dropped: int
    #: Additive 2026-08-24; defaulted so every existing keyword construction
    #: (including test fakes) keeps working unchanged.
    eligible_end: int | None = None

    @property
    def effective(self) -> bool:
        """Did this call actually shrink the prompt?

        Derived, never stored, so it can't drift from the fields it reads.

        The 2026-08-24 forensics turned on this distinction: a
        `compaction_phase_reached phase_reached=3` line reads as the most
        aggressive tier responding to pressure, but the same record carried
        `tokens_before == tokens_after == 71616` and `tool_results_dropped=0`.
        Phase 3 fired and achieved NOTHING — `_find_eligible_end` returns 2
        when fewer than `keep_recent` assistant messages exist, which is every
        early step of a chat turn. Telemetry that cannot say "this did
        nothing" is how that hid for months.

        Phase 0 (no phase attempted) is reported False, which is accurate: the
        loop only emits the event when `phase_reached > 0`.
        """
        return self.tokens_after < self.tokens_before or self.tool_results_dropped > 0


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

        result, dropped = self._phase1(messages, eligible_end)
        tokens_after = estimate_messages_tokens(result)
        if tokens_after < t2:
            return CompactionResult(
                messages=result,
                phase_reached=1,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                tool_results_dropped=dropped,
                eligible_end=eligible_end,
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
                eligible_end=eligible_end,
            )

        result, dropped = self._phase3(messages, eligible_end)
        tokens_after = estimate_messages_tokens(result)
        return CompactionResult(
            messages=result,
            phase_reached=3,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tool_results_dropped=dropped,
            eligible_end=eligible_end,
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
