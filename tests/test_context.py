"""Tests for context pressure monitoring + TieredCompact (forge-hybrid Phase 2 A)."""

from luxe.context import (
    CompactionResult,
    TieredCompact,
    context_pressure,
    elide_old_tool_results,
    estimate_messages_tokens,
    estimate_tokens,
)


# ── existing tests ──────────────────────────────────────────────────────


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") == 2  # 11 chars // 4


def test_context_pressure_empty():
    assert context_pressure([], 8192) == 0.0


def test_context_pressure_calculation():
    messages = [{"role": "user", "content": "x" * 4000}]
    pressure = context_pressure(messages, 2000)
    assert pressure > 0.4


def test_elide_below_threshold():
    messages = [
        {"role": "user", "content": "short"},
        {"role": "tool", "name": "read_file", "content": "data"},
    ]
    result = elide_old_tool_results(messages, 100000)
    assert result[1]["content"] == "data"  # not elided


def test_elide_above_threshold():
    big_content = "x" * 10000
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "name": "read_file", "content": big_content},
        {"role": "tool", "name": "grep", "content": big_content},
        {"role": "tool", "name": "read_file", "content": big_content},
        {"role": "tool", "name": "grep", "content": big_content},
        {"role": "tool", "name": "read_file", "content": "keep1"},
        {"role": "tool", "name": "read_file", "content": "keep2"},
        {"role": "tool", "name": "read_file", "content": "keep3"},
        {"role": "tool", "name": "read_file", "content": "keep4"},
    ]
    result = elide_old_tool_results(messages, 1000, threshold=0.1)
    assert "[elided:" in result[1]["content"]
    assert result[-1]["content"] == "keep4"  # recent kept


# ── TieredCompact (forge-hybrid Phase 2 A) ─────────────────────────────


def _build_deep_trajectory(
    *,
    n_iterations: int,
    tool_result_size: int = 200,
    assistant_text_size: int = 50,
    nudge_indices: tuple[int, ...] = (),
) -> list[dict]:
    """Construct a synthetic trajectory: system + task + N (assistant→tool) iterations.

    Each iteration adds:
      - 1 assistant message with `tool_calls` + `content`=`assistant_text_size` chars
      - 1 tool result with `content`=`tool_result_size` chars
    Indices in `nudge_indices` get a user-role _luxe_nudge inserted before the
    assistant message of that iteration.
    """
    messages: list[dict] = [
        {"role": "system", "content": "you are an agent"},
        {"role": "user", "content": "fix the bug"},
    ]
    for i in range(n_iterations):
        if i in nudge_indices:
            messages.append({
                "role": "user",
                "content": "Mid-loop notice: write something now.",
                "_luxe_nudge": True,
                "_luxe_nudge_type": "write_pressure",
            })
        messages.append({
            "role": "assistant",
            "content": "x" * assistant_text_size,
            "tool_calls": [{
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"a"}'},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"call_{i}",
            "name": "read_file",
            "content": "y" * tool_result_size,
        })
    return messages


def test_tiered_compact_below_threshold_returns_unchanged():
    """Phase 0: when tokens < threshold, messages pass through unchanged."""
    messages = _build_deep_trajectory(n_iterations=2, tool_result_size=20)
    cr = TieredCompact().compact(messages, ctx_limit=100_000)
    assert cr.phase_reached == 0
    assert cr.messages == messages
    assert cr.tool_results_dropped == 0
    assert cr.tokens_before == cr.tokens_after


def test_tiered_compact_protected_messages_never_dropped():
    """messages[0] (system) and messages[1] (task) must survive all phases."""
    messages = _build_deep_trajectory(n_iterations=20, tool_result_size=4000)
    cr = TieredCompact(keep_recent=2, compact_threshold=0.1).compact(
        messages, ctx_limit=1000
    )
    assert cr.phase_reached >= 1, "compaction should have fired"
    assert cr.messages[0]["role"] == "system"
    assert cr.messages[0]["content"] == "you are an agent"
    assert cr.messages[1]["role"] == "user"
    assert cr.messages[1]["content"] == "fix the bug"


def test_tiered_compact_phase1_drops_nudges_truncates_tool_results():
    """Phase 1 fire: _luxe_nudge messages dropped + tool_results truncated.

    Sized so that phase 1 alone (drop nudges + truncate) is enough to fall
    below the trigger — phase_reached must be exactly 1.
    """
    messages = _build_deep_trajectory(
        n_iterations=5,
        tool_result_size=1500,
        nudge_indices=(0, 1, 2, 3, 4),
    )
    # ctx_limit=4000 → trigger=2000 tokens. tokens_before ~ 2184; phase 1
    # truncation saves ~1000 tokens → ~1170 tokens < 2000 → phase 1 wins.
    cr = TieredCompact(keep_recent=2, compact_threshold=0.5).compact(
        messages, ctx_limit=4000
    )
    assert cr.phase_reached == 1, f"expected exact phase 1; got {cr.phase_reached}"
    # All eligible nudges (in indices 2..eligible_end) should be gone.
    # keep_recent=2 protects last 2 iterations; the last 2 nudges may survive.
    nudges_remaining = [m for m in cr.messages if m.get("_luxe_nudge")]
    assert len(nudges_remaining) <= 2, (
        f"early nudges should be dropped; got {len(nudges_remaining)}"
    )
    # Truncated tool_results carry a "[Truncated" marker.
    truncated = [
        m for m in cr.messages
        if m.get("role") == "tool" and "[Truncated" in (m.get("content") or "")
    ]
    assert truncated, "phase 1 should truncate at least one tool_result"


def test_tiered_compact_phase2_drops_tool_results_entirely():
    """Phase 2 fire: tool_results dropped (not just truncated)."""
    # Force phase 2 by making tool_results numerous + large enough that
    # phase 1 truncation alone doesn't get below threshold.
    messages = _build_deep_trajectory(n_iterations=30, tool_result_size=8000)
    cr = TieredCompact(keep_recent=2, compact_threshold=0.5).compact(
        messages, ctx_limit=500
    )
    assert cr.phase_reached >= 2, (
        f"phase 2 should fire on heavy trajectory; got phase {cr.phase_reached}"
    )
    # Eligible-region tool_results should be gone (only keep_recent's tools
    # survive). With 30 iterations and keep_recent=2, expect at most ~2 tools.
    surviving_tool_results = [m for m in cr.messages if m.get("role") == "tool"]
    assert len(surviving_tool_results) <= 2


def test_tiered_compact_phase3_drops_text_clears_tool_call_content():
    """Phase 3 fire: text-response messages dropped + tool_call content cleared."""
    # Force phase 3 by making EVERY iteration carry large assistant text.
    messages = _build_deep_trajectory(
        n_iterations=20,
        tool_result_size=8000,
        assistant_text_size=5000,
    )
    cr = TieredCompact(keep_recent=2, compact_threshold=0.5).compact(
        messages, ctx_limit=300
    )
    assert cr.phase_reached == 3
    # Eligible-region assistant messages had their content cleared
    # (they were tool_call messages, so they survive but with content="").
    eligible_assistants = [
        m for m in cr.messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    # The last keep_recent assistants are intact; earlier ones have content="".
    cleared_count = sum(1 for m in eligible_assistants if m.get("content") == "")
    assert cleared_count >= 1, "phase 3 should clear at least one assistant content"


def test_tiered_compact_drops_luxe_repair_marker():
    """_luxe_repair (BFCL reflect marker) is recognized as a nudge too."""
    messages = _build_deep_trajectory(n_iterations=10, tool_result_size=2000)
    # Inject a _luxe_repair message in the eligible region.
    repair_msg = {
        "role": "user",
        "content": "complete it now",
        "_luxe_repair": True,
    }
    messages.insert(4, repair_msg)
    cr = TieredCompact(keep_recent=2, compact_threshold=0.5).compact(
        messages, ctx_limit=2000
    )
    assert cr.phase_reached >= 1
    assert not any(m.get("_luxe_repair") for m in cr.messages), (
        "_luxe_repair must be treated as a droppable nudge"
    )


def test_tiered_compact_keep_recent_protects_last_iterations():
    """keep_recent=N protects the last N assistant boundaries fully."""
    messages = _build_deep_trajectory(n_iterations=10, tool_result_size=2000)
    cr = TieredCompact(keep_recent=3, compact_threshold=0.5).compact(
        messages, ctx_limit=1500
    )
    assert cr.phase_reached >= 1
    # The last 3 (assistant, tool) pairs must be present untouched.
    # Find assistant indices in the original to identify their last 3.
    original_assistant_contents = [
        m["content"] for m in messages if m.get("role") == "assistant"
    ]
    compacted_assistant_contents = [
        m.get("content", "") for m in cr.messages if m.get("role") == "assistant"
    ]
    # The last 3 assistant contents from the original must appear in the
    # compacted output, IN ORDER, and be the LAST 3 assistants there.
    last_3_original = original_assistant_contents[-3:]
    last_3_compacted = compacted_assistant_contents[-3:]
    assert last_3_compacted == last_3_original


def test_tiered_compact_telemetry_payload():
    """CompactionResult carries accurate before/after token counts + drop count."""
    messages = _build_deep_trajectory(n_iterations=15, tool_result_size=3000)
    cr = TieredCompact(keep_recent=2, compact_threshold=0.5).compact(
        messages, ctx_limit=1000
    )
    assert isinstance(cr, CompactionResult)
    assert cr.phase_reached >= 1
    assert cr.tokens_before > cr.tokens_after  # compaction reduced tokens
    assert cr.tool_results_dropped >= 1
    # tokens_before should match a fresh estimate of the input.
    assert cr.tokens_before == estimate_messages_tokens(messages)
    # tokens_after should match a fresh estimate of the output.
    assert cr.tokens_after == estimate_messages_tokens(cr.messages)


def test_tiered_compact_default_keep_recent_is_3():
    """keep_recent default is 3 (matches the forge-hybrid plan)."""
    tc = TieredCompact()
    assert tc.keep_recent == 3
    assert tc.compact_threshold == 0.75


def test_tiered_compact_zero_ctx_limit_no_op():
    """ctx_limit <= 0 returns unchanged with phase=0 (defensive)."""
    messages = _build_deep_trajectory(n_iterations=5)
    cr = TieredCompact().compact(messages, ctx_limit=0)
    assert cr.phase_reached == 0
    assert cr.messages == messages


def test_tiered_compact_short_trajectory_protects_everything():
    """When fewer than keep_recent iterations exist, eligible_end is 2 (nothing eligible)."""
    messages = _build_deep_trajectory(n_iterations=2, tool_result_size=20000)
    cr = TieredCompact(keep_recent=3, compact_threshold=0.1).compact(
        messages, ctx_limit=500
    )
    # Compaction tries but eligible_end=2 means no messages are in [2, 2) range.
    # Phase 1/2/3 would all return the same content; phase reported >= 1 but
    # no actual drops happen.
    assert all(
        m.get("content") == orig.get("content")
        for m, orig in zip(cr.messages, messages)
    )
