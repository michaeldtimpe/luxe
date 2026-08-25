"""Tests for context pressure monitoring + TieredCompact (forge-hybrid Phase 2 A)."""

import pytest

from luxe.context import (
    CALIBRATION_UNMEASURED_RATIO,
    TOOL_RESULT_CLAMP_FLOOR_CHARS,
    CompactionResult,
    TieredCompact,
    clamp_tool_result,
    context_pressure,
    damped_calibration,
    elide_old_tool_results,
    estimate_messages_tokens,
    estimate_tokens,
    tool_result_clamp_chars,
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


def test_tiered_compact_phase_thresholds_overrides_single_threshold():
    """phase_thresholds tuple overrides compact_threshold; each phase fires at own trigger."""
    tc = TieredCompact(
        keep_recent=2,
        compact_threshold=0.75,
        phase_thresholds=(0.50, 0.85, 0.95),
    )
    # Internal triggers reflect the tuple, not the single threshold.
    assert tc._phase_triggers == (0.50, 0.85, 0.95)


def test_tiered_compact_default_phase_thresholds_shipped_aggressive_p1_conservative_p3():
    """When phase_thresholds is None, falls back to the SHIPPED default
    (0.50, 0.85, 0.95) — the forge-hybrid 2026-05-28 default-ON tuning.
    This default replaced the 2026-05-27 single-threshold fallback after
    the n=75 rep-1+rep-2 validation showed aggressive phase 1 captures
    protected wrong_target heals while conservative phase 3 avoids the
    reasoning-drop destruction mode."""
    tc = TieredCompact(keep_recent=2, compact_threshold=0.60)
    # compact_threshold is no longer the fallback when phase_thresholds=None;
    # the locked default tuple is shipped instead.
    assert tc._phase_triggers == (0.50, 0.85, 0.95)
    assert tc._phase_triggers == TieredCompact._DEFAULT_PHASE_THRESHOLDS


def test_tiered_compact_phase_thresholds_explicit_override_wins():
    """When the caller passes phase_thresholds explicitly, it wins over the
    shipped default (and over compact_threshold)."""
    tc = TieredCompact(
        keep_recent=2,
        compact_threshold=0.60,
        phase_thresholds=(0.40, 0.70, 0.90),
    )
    assert tc._phase_triggers == (0.40, 0.70, 0.90)


def test_tiered_compact_phase_thresholds_aggressive_p1_conservative_p3():
    """Aggressive phase 1 (0.50) + conservative phase 3 (0.95) — capture phase 1 wins
    without phase 3 destruction. The forge-hybrid Phase 2 (A) n=75 4-arm finding."""
    # Build a trajectory that exceeds phase 1 trigger but not phase 3.
    messages = _build_deep_trajectory(
        n_iterations=8,
        tool_result_size=2000,
        nudge_indices=(0, 1, 2, 3, 4, 5),
    )
    # ctx_limit=4000 → triggers: p1=2000, p2=3400, p3=3800.
    # tokens_before is well above 2000 so phase 1 fires.
    # Phase 1's truncation alone should bring tokens below the lenient
    # phase 2 trigger (3400) — so it stops at phase 1.
    cr = TieredCompact(
        keep_recent=2,
        phase_thresholds=(0.50, 0.85, 0.95),
    ).compact(messages, ctx_limit=4000)
    assert cr.phase_reached == 1, f"expected phase 1 fire with aggressive p1; got {cr.phase_reached}"
    # And nudges should be gone from the eligible region.
    nudges_remaining = [m for m in cr.messages if m.get("_luxe_nudge")]
    assert len(nudges_remaining) <= 2  # keep_recent=2 protects last 2 iterations


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


# ── the compaction no-op is now visible (2026-08-24, additive telemetry) ──
#
# `acceptance/chat_bigread_2026_08_24/EVIDENCE.md`: a chat turn recorded
# `compaction_phase_reached phase_reached=3 tokens_before=71616
# tokens_after=71616 tool_results_dropped=0`. The most aggressive tier fired
# and achieved nothing — `_find_eligible_end` returns 2 when fewer than
# `keep_recent` assistant messages exist, which is every early step of a chat
# turn. The record read as a response to pressure. These pin that it can no
# longer.


def test_a_phase_that_fires_on_a_short_trajectory_reports_it_achieved_nothing():
    """The EVIDENCE.md shape, reproduced: 2 assistant messages, way over the
    phase-3 trigger, nothing eligible, nothing dropped."""
    messages = _build_deep_trajectory(n_iterations=2, tool_result_size=20_000)
    cr = TieredCompact(keep_recent=3).compact(messages, ctx_limit=1000)

    assert cr.phase_reached == 3           # the most aggressive tier fired
    assert cr.tokens_before == cr.tokens_after
    assert cr.tool_results_dropped == 0
    assert cr.effective is False           # ...and achieved nothing
    assert cr.eligible_end == 2            # because nothing was eligible


def test_a_phase_that_actually_compacts_reports_effective():
    messages = _build_deep_trajectory(n_iterations=15, tool_result_size=3000)
    cr = TieredCompact(keep_recent=2, compact_threshold=0.5).compact(
        messages, ctx_limit=1000
    )
    assert cr.phase_reached >= 1
    assert cr.effective is True
    assert cr.eligible_end is not None and cr.eligible_end > 2


def test_effective_is_derived_not_stored():
    """A property, so it can never drift from the fields it reads — and so
    adding it could not change any existing constructor call."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(CompactionResult)}
    assert "effective" not in fields
    # Every pre-2026-08-24 construction site passed exactly these five.
    cr = CompactionResult(messages=[], phase_reached=2, tokens_before=1000,
                          tokens_after=950, tool_results_dropped=1)
    assert cr.eligible_end is None
    assert cr.effective is True


def test_a_drop_with_no_token_change_still_counts_as_effective():
    """Truncating a tool result that was already short can leave the estimate
    flat; the drop itself is still work done."""
    cr = CompactionResult(messages=[], phase_reached=1, tokens_before=100,
                          tokens_after=100, tool_results_dropped=2)
    assert cr.effective is True


def test_no_phase_reached_reports_nothing_achieved():
    messages = _build_deep_trajectory(n_iterations=2, tool_result_size=20)
    cr = TieredCompact().compact(messages, ctx_limit=100_000)
    assert cr.phase_reached == 0
    assert cr.effective is False
    assert cr.eligible_end is None       # no phase ran, so none was computed


def test_the_pinned_compaction_knobs_are_untouched():
    """3.3 is observability ONLY. `keep_recent`, the phase thresholds, and
    `_find_eligible_end`'s semantics keep their pinned values (agents.sdd
    'forge-hybrid Phase 2 (A) compaction invariants')."""
    tc = TieredCompact()
    assert tc.keep_recent == 3
    assert tc._phase_triggers == (0.50, 0.85, 0.95)
    assert TieredCompact._DEFAULT_PHASE_THRESHOLDS == (0.50, 0.85, 0.95)
    short = _build_deep_trajectory(n_iterations=2)
    assert TieredCompact._find_eligible_end(short, 3) == 2


# ── single tool-result clamp (LUXE_TOOL_RESULT_CLAMP, 2026-08-24) ────────


def _read_file_output(n_lines: int, width: int = 60, offset: int = 0) -> str:
    """`tools.fs`'s exact numbering: `f"{i + offset + 1}\\t{line}"`."""
    return "".join(f"{i + offset + 1}\t{'c' * width}\n" for i in range(n_lines))


class TestToolResultClampChars:
    def test_it_is_estimate_tokens_inverse_times_the_share(self):
        # 100_000 tokens * 0.25 * 4 chars/token
        assert tool_result_clamp_chars(100_000) == 100_000

    def test_it_floors_so_a_tiny_window_can_still_read_a_source_file(self):
        assert tool_result_clamp_chars(1000) == TOOL_RESULT_CLAMP_FLOOR_CHARS

    def test_a_non_positive_limit_means_no_bound(self):
        assert tool_result_clamp_chars(0) == 0
        assert tool_result_clamp_chars(-1) == 0

    def test_it_agrees_with_the_read_budget_at_the_same_window(self):
        """Passed the CALIBRATED limit, it lands on the same number
        `tools.fs.budget_for_ctx` reaches from the uncalibrated side — the two
        budgets must not disagree about what one result is worth."""
        from luxe.context import calibrated_ctx_limit
        from luxe.tools.fs import budget_for_ctx

        assert tool_result_clamp_chars(
            calibrated_ctx_limit(32768, 2.4)) == budget_for_ctx(32768)


class TestClampTooLargeResult:
    def test_a_result_that_fits_is_returned_untouched(self):
        body = "x" * 100
        got, dropped = clamp_tool_result(body, tool_name="bash", max_chars=8192)
        assert got is body and dropped == 0

    def test_a_zero_budget_is_no_bound(self):
        body = "x" * 100_000
        got, dropped = clamp_tool_result(body, tool_name="bash", max_chars=0)
        assert got is body and dropped == 0

    def test_bash_output_is_cut_and_says_the_loss_is_not_recoverable(self):
        """`bash`, `grep` and MCP tools have NO budget of their own — this is
        the case the clamp exists for. There is no `offset=` to offer, so the
        trailer must not imply one."""
        body = "".join(f"line {i}\n" for i in range(20_000))
        got, dropped = clamp_tool_result(body, tool_name="bash", max_chars=5_000)

        assert dropped > 0
        assert len(got) < len(body)
        assert "NOT recoverable" in got
        assert "bash" in got
        assert f"{dropped:,}" in got and f"{len(body):,}" in got
        assert "offset=" not in got          # no invented resume
        assert "read_file(" not in got

    def test_grep_output_is_cut_the_same_way(self):
        body = "".join(f"src/a.py:{i}: match\n" for i in range(20_000))
        got, dropped = clamp_tool_result(body, tool_name="grep", max_chars=4_096)
        assert dropped > 0
        assert "grep" in got and "offset=" not in got

    def test_read_file_output_carries_a_TRUE_resume_offset(self):
        """`read_file` numbers its lines, so the offset is recovered from what
        survived rather than guessed — the same shape `tools.fs` emits."""
        body = _read_file_output(4_000)
        got, dropped = clamp_tool_result(
            body, tool_name="read_file", max_chars=5_000, path="self.md")

        assert dropped > 0
        assert 'read_file(path="self.md", offset=' in got
        # The offset must be the line number the model has NOT seen yet.
        import re as _re
        offset = int(_re.search(r"offset=(\d+)", got).group(1))
        kept_lines = [ln for ln in got.splitlines()
                      if _re.match(r"^\d+\t", ln)]
        assert offset == int(kept_lines[-1].split("\t")[0])
        assert f"{offset + 1}\t" not in got     # the next line really is unseen

    def test_it_never_hands_back_a_half_written_line(self):
        body = _read_file_output(4_000)
        got, _ = clamp_tool_result(body, tool_name="read_file",
                                   max_chars=5_003, path="a.py")
        head = got.split("\n[truncated")[0]
        assert head.endswith("\n")

    def test_read_file_without_a_path_falls_back_to_the_honest_wording(self):
        """No path means no resume can be constructed. Say so; never guess."""
        body = _read_file_output(4_000)
        got, dropped = clamp_tool_result(body, tool_name="read_file",
                                         max_chars=5_000, path=None)
        assert dropped > 0
        assert "NOT recoverable" in got and "offset=" not in got

    def test_unnumbered_read_file_content_gets_no_invented_offset(self):
        body = "x" * 50_000
        got, dropped = clamp_tool_result(body, tool_name="read_file",
                                         max_chars=5_000, path="a.py")
        assert dropped > 0
        assert "offset=" not in got

    def test_the_clamped_result_actually_fits_the_budget_it_names(self):
        """The trailer is added on top, so assert the payload — not the whole
        string — respects the bound, and that the whole string stays within a
        small, bounded overhead of it."""
        body = "z" * 200_000
        got, _ = clamp_tool_result(body, tool_name="bash", max_chars=10_000)
        payload = got.split("\n[truncated")[0]
        assert len(payload) <= 10_000
        assert len(got) < 10_000 + 500


# ── calibration extrapolation damping (LUXE_CTX_CAL_DAMP, 2026-08-24) ────
#
# The ratio is measured on the PREVIOUS response. At step 1 that prompt is
# system-prompt-and-tool-JSON (observed 1.79-2.64x); applied to a next prompt
# that is 96% prose (1.21-1.27x in the SAME sessions) it reported 102.5%
# pressure on a request that was ~65% of the window, and the failure was
# blamed on the endpoint. `acceptance/chat_bigread_2026_08_24/EVIDENCE.md`
# finding 3.


class TestDampedCalibration:
    def test_the_168f1825a1fd_replay(self):
        """The live numbers. A 1.88x ratio measured on a ~1,650-token prompt,
        applied to a 71,616-token estimate on a 128K window: 102.5% before,
        ~66% after — next to the prose-true ~1.2x reading, and no longer
        anywhere near the 0.95 phase-3 trigger."""
        undamped = 1.88
        damped = damped_calibration(undamped, 1_650, 71_616)

        assert 1.15 < damped < 1.30                     # the prose-true band
        assert 71_616 * undamped / 131_072 > 1.00       # what it used to say
        assert 0.60 < 71_616 * damped / 131_072 < 0.70  # what it says now

    def test_gradual_growth_keeps_almost_all_of_the_correction(self):
        """The SWE-bench / maintain shape: a prompt that grew 10% carries a
        ratio that is still 90% about the same material. Damping must not
        quietly undo server-truth calibration on the benchmark path."""
        got = damped_calibration(1.90, 10_000, 11_000)
        assert got == pytest.approx(1.90 - (1.90 - 1.2) * (1 - 10 / 11), abs=1e-9)
        assert got > 1.83

    def test_a_prompt_that_did_not_grow_is_not_damped_at_all(self):
        assert damped_calibration(1.90, 10_000, 10_000) == 1.90

    def test_a_prompt_that_shrank_is_not_damped_at_all(self):
        """Post-compaction the estimate can fall below the calibration sample.
        The share is capped at 1.0 — never used to AMPLIFY a ratio."""
        assert damped_calibration(1.90, 10_000, 4_000) == 1.90

    def test_an_uncalibrated_step_is_returned_untouched(self):
        """Step 1, and every backend that reports no usage, sit at exactly
        1.0. Damping must never move them off it — that would be inventing a
        correction where the contract says degrade to none."""
        assert damped_calibration(1.0, 1_650, 71_616) == 1.0
        assert damped_calibration(1.0, 0, 71_616) == 1.0

    def test_no_measurement_means_no_damping(self):
        assert damped_calibration(1.88, 0, 71_616) == 1.88
        assert damped_calibration(1.88, 1_650, 0) == 1.88

    def test_it_only_ever_moves_toward_one_never_past_it(self):
        """The guard that keeps the unmeasured-material constant from
        INFLATING a ratio that already sits below it."""
        assert damped_calibration(1.10, 10, 1_000_000) == pytest.approx(1.10)
        below = damped_calibration(0.60, 10, 1_000_000)
        assert 0.60 <= below <= 1.0

    def test_the_pinned_clamp_survives_by_construction(self):
        """agents.sdd pins [0.5, 8.0]. The result always lies between the
        input ratio and 1.0, so it cannot leave the band."""
        from luxe.context import CALIBRATION_MAX, CALIBRATION_MIN

        for cal in (CALIBRATION_MIN, 0.7, 1.0, 1.2, 1.88, 4.0, CALIBRATION_MAX):
            for est_now in (1, 100, 71_616, 10 ** 7):
                got = damped_calibration(cal, 1_650, est_now)
                assert CALIBRATION_MIN <= got <= CALIBRATION_MAX
                lo, hi = min(cal, 1.0), max(cal, 1.0)
                assert lo - 1e-9 <= got <= hi + 1e-9

    def test_non_finite_and_degenerate_ratios_pass_through(self):
        """Unreachable through `calibration_ratio`'s clamp, but a pressure
        calculation must never be the thing that kills a run."""
        import math

        assert math.isnan(damped_calibration(float("nan"), 1_650, 71_616))
        assert damped_calibration(float("inf"), 1_650, 71_616) == float("inf")
        assert damped_calibration(0.0, 1_650, 71_616) == 0.0
        assert damped_calibration(-1.0, 1_650, 71_616) == -1.0

    def test_the_unmeasured_ratio_is_sweepable_without_editing_source(self):
        """The promotion bench sweeps 1.0 / 1.2 / 1.6 so the constant is chosen
        by evidence rather than inherited from one session. A knob benched in a
        form that cannot move is the C10 trap."""
        assert damped_calibration(1.88, 1_650, 71_616, unmeasured=1.0) < \
            damped_calibration(1.88, 1_650, 71_616)
        assert damped_calibration(1.88, 1_650, 71_616, unmeasured=1.6) > \
            damped_calibration(1.88, 1_650, 71_616)
        # An infinitely-outgrown sample converges on whatever it is given.
        assert damped_calibration(8.0, 1, 10 ** 9, unmeasured=1.6) == \
            pytest.approx(1.6, abs=1e-6)

    def test_an_overridden_ratio_keeps_every_safety_property(self):
        """The sweep must not be able to break the two invariants: the result
        still only moves toward 1.0, and still cannot leave [0.5, 8.0]."""
        from luxe.context import CALIBRATION_MAX, CALIBRATION_MIN

        for unmeasured in (CALIBRATION_MIN, 1.0, 1.2, 1.6, 2.4, CALIBRATION_MAX):
            for cal in (0.6, 1.0, 1.1, 1.88, 8.0):
                got = damped_calibration(cal, 1_650, 71_616,
                                         unmeasured=unmeasured)
                assert CALIBRATION_MIN <= got <= CALIBRATION_MAX
                lo, hi = min(cal, 1.0), max(cal, 1.0)
                assert lo - 1e-9 <= got <= hi + 1e-9
        # Including the one that would otherwise inflate an uncalibrated step.
        assert damped_calibration(1.0, 1_650, 71_616, unmeasured=2.4) == 1.0

    def test_the_default_argument_is_the_module_constant(self):
        """No caller has to know the number; the loop passes RunFlags' value
        and everything else inherits the measured one."""
        assert damped_calibration(1.88, 1_650, 71_616) == \
            damped_calibration(1.88, 1_650, 71_616,
                               unmeasured=CALIBRATION_UNMEASURED_RATIO)

    def test_the_unmeasured_ratio_is_the_measured_prose_value(self):
        """Cited, not guessed: the same sessions recalibrated to 1.27x and
        1.21x once a prose file landed (EVIDENCE.md finding 3)."""
        assert CALIBRATION_UNMEASURED_RATIO == 1.2
        # An infinitely-outgrown sample converges on it exactly.
        assert damped_calibration(8.0, 1, 10 ** 9) == pytest.approx(1.2, abs=1e-6)
