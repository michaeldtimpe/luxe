"""`scripts/toolcall_taxonomy.py` — the C1 mining script.

A mining script that miscounts is worse than none: the first draft's
hand-maintained tool list reported 342 phantom "unknown tool" dispatches
because it omitted three real tools. These tests pin the counting rules
against a synthetic `~/.luxe` so the numbers in
`acceptance/toolcall_taxonomy_2026_08/` mean what they say.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import toolcall_taxonomy as tt  # noqa: E402


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


@pytest.fixture
def luxe(tmp_path: Path) -> Path:
    return tmp_path / ".luxe"


def _now() -> float:
    return time.time()


def _run(luxe: Path, run_id: str, recs: list[dict]) -> None:
    for r in recs:
        r.setdefault("ts", _now())
        r.setdefault("run_id", run_id)
    _write(luxe / "runs" / run_id / "events.jsonl", recs)


def _session(luxe: Path, sid: str, recs: list[dict]) -> None:
    for r in recs:
        r.setdefault("ts", _now())
    _write(luxe / "sessions" / sid / "transcript.jsonl", recs)


def _collect(luxe: Path, days: int = 45, **kw):
    return tt.collect(luxe, time.time() - days * 86400, **kw)


class TestKnownTools:
    def test_the_live_registry_is_preferred(self):
        names, provenance = tt.known_tools()
        assert "live registry" in provenance
        # The three the first draft got wrong.
        assert {"cve_lookup", "git_show", "deps_audit"} <= names
        assert {"read_file", "edit_file", "bash"} <= names

    def test_the_fallback_snapshot_matches_the_live_set(self):
        """A stale snapshot silently produces a wrong denominator."""
        live, _ = tt.known_tools()
        missing = sorted(live - tt._FALLBACK_TOOLS)
        assert missing == [], f"_FALLBACK_TOOLS is stale, missing: {missing}"


class TestUnknownToolNames:
    def test_a_real_tool_is_not_flagged(self, luxe):
        _run(luxe, "s1-0", [{"kind": "tool_call", "name": "cve_lookup"}])
        b, _ = _collect(luxe)
        assert b["unknown_tool_name"].count == 0

    def test_a_hallucinated_name_is_flagged(self, luxe):
        _run(luxe, "s1-0", [{"kind": "tool_call", "name": "final_report"}])
        b, _ = _collect(luxe)
        assert b["unknown_tool_name"].count == 1
        assert b["unknown_tool_name"].by_key["final_report"] == 1

    def test_whitespace_suffixed_names_are_stripped_like_the_loop_does(self, luxe):
        """loop.py:1403 strips the name; a pre-strip artefact is not an
        unknown tool."""
        _run(luxe, "s1-0", [{"kind": "tool_call", "name": "read_file\n\n"}])
        b, _ = _collect(luxe)
        assert b["unknown_tool_name"].count == 0

    def test_mcp_namespaced_tools_are_known(self, luxe):
        _run(luxe, "s1-0", [{"kind": "tool_call", "name": "mcp__kappa__ls"}])
        b, _ = _collect(luxe)
        assert b["unknown_tool_name"].count == 0


class TestApparatusExclusion:
    def test_harness_runs_are_skipped_by_default(self, luxe):
        _run(luxe, "ccab-x-r1-c1", [{"kind": "tool_call", "name": "foo"}] * 9)
        b, stats = _collect(luxe)
        assert b["unknown_tool_name"].count == 0
        assert stats["runs_skipped_apparatus"] == 1

    def test_include_apparatus_restores_them(self, luxe):
        _run(luxe, "ccab-x-r1-c1", [{"kind": "tool_call", "name": "foo"}] * 9)
        b, _ = _collect(luxe, include_apparatus=True)
        assert b["unknown_tool_name"].count == 9


class TestWindow:
    def test_records_outside_the_window_are_ignored(self, luxe):
        old = time.time() - 100 * 86400
        _run(luxe, "s1-0", [{"kind": "tool_call", "name": "final_report",
                             "ts": old}])
        b, _ = _collect(luxe, days=45)
        assert b["unknown_tool_name"].count == 0
        b2, _ = _collect(luxe, days=365)
        assert b2["unknown_tool_name"].count == 1


class TestOtherBuckets:
    def test_schema_rejects_come_from_single_mode_done(self, luxe):
        _run(luxe, "s1-0", [{"kind": "single_mode_done", "schema_rejects": 3}])
        b, _ = _collect(luxe)
        assert b["schema_reject"].count == 3

    def test_duplicate_storm_needs_three_duplicates_in_one_run(self, luxe):
        _run(luxe, "s1-0", [{"kind": "tool_call", "name": "read_file",
                             "duplicate": True}] * 2)
        _run(luxe, "s2-0", [{"kind": "tool_call", "name": "read_file",
                             "duplicate": True}] * 4)
        b, _ = _collect(luxe)
        assert b["duplicate_storm"].count == 1
        assert b["duplicate_storm"].sessions == {"s2"}

    def test_aborted_runs_are_keyed_by_reason(self, luxe):
        _run(luxe, "s1-0", [{"kind": "single_mode_done", "aborted": True,
                             "abort_reason": "Max steps reached (30)"}])
        b, _ = _collect(luxe)
        assert b["aborted_run"].by_key["Max steps reached (30)"] == 1

    def test_empty_completed_turns_are_counted_but_interrupted_ones_are_not(
            self, luxe):
        _session(luxe, "s1", [
            {"kind": "user", "text": "q"},
            {"kind": "assistant", "text": "", "run_id": "s1-0",
             "interrupted": False, "steps": 3},
            {"kind": "user", "text": "q2"},
            {"kind": "assistant", "text": "", "run_id": "s1-1",
             "interrupted": True},
        ])
        b, _ = _collect(luxe)
        assert b["empty_response"].count == 1

    def test_textfallback_drop_needs_a_tool_call_tag_and_no_tool_event(self, luxe):
        _session(luxe, "s1", [
            {"kind": "user", "text": "q"},
            {"kind": "assistant", "run_id": "s1-0",
             "text": 'sure: <tool_call>{"name":"nope"}</tool_call>'},
        ])
        b, _ = _collect(luxe)
        assert b["textfallback_drop"].count == 1

    def test_a_turn_with_real_tool_events_is_not_a_drop(self, luxe):
        _run(luxe, "s1-0", [{"kind": "tool_call", "name": "read_file"}])
        _session(luxe, "s1", [
            {"kind": "user", "text": "q"},
            {"kind": "assistant", "run_id": "s1-0",
             "text": 'quoting <tool_call>{"name":"x"}</tool_call> back at you'},
        ])
        b, _ = _collect(luxe)
        assert b["textfallback_drop"].count == 0

    def test_error_records_are_counted_by_type(self, luxe):
        _session(luxe, "s1", [
            {"kind": "error", "text": "BackendError: oMLX returned 500"},
        ])
        b, _ = _collect(luxe)
        assert b["turn_error"].count == 1
        assert b["turn_error"].by_key["BackendError"] == 1


class TestEvidenceBar:
    def test_bar_needs_both_occurrences_and_distinct_sessions(self, luxe):
        # 9 occurrences, ONE session — count clears, sessions don't.
        _run(luxe, "s1-0", [{"kind": "tool_call", "name": "final_report"}] * 9)
        b, _ = _collect(luxe)
        assert b["unknown_tool_name"].count == 9
        assert not b["unknown_tool_name"].clears_bar
        assert "below bar" in b["unknown_tool_name"].verdict

    def test_bar_clears_with_both(self, luxe):
        for sid in ("s1", "s2", "s3"):
            _run(luxe, f"{sid}-0",
                 [{"kind": "tool_call", "name": "final_report"}] * 2)
        b, _ = _collect(luxe)
        assert b["unknown_tool_name"].clears_bar
        assert b["unknown_tool_name"].verdict == "CLEARS BAR"


class TestRendering:
    def test_a_degenerate_key_cannot_break_the_table(self):
        """A mined name was `list_dir` + hundreds of newlines — model output
        is never trustworthy as one line."""
        shown = tt._display("list_dir" + "\n" * 400)
        assert "\n" not in shown and len(shown) <= 71

    def test_report_renders_and_names_the_bar(self, luxe):
        _run(luxe, "s1-0", [{"kind": "tool_call", "name": "read_file"}])
        b, stats = _collect(luxe)
        out = tt.render(b, stats, days=45, luxe_root=luxe)
        assert "# Tool-call taxonomy" in out
        assert "Evidence bar" in out and "No class clears the evidence bar" in out

    def test_context_render_is_labelled_as_not_counting(self, luxe):
        b, stats = _collect(luxe, days=400)
        out = tt.render_context(b, stats, days=400)
        assert "NOT counted against the bar" in out

    def test_read_only_over_a_missing_root(self, tmp_path):
        assert tt.main(["--luxe-root", str(tmp_path / "nope")]) == 1


class TestDirectEvents:
    """2026-08-04 follow-up: the loop emits `tool_reject` and
    `textfallback_drop` directly. Direct events are preferred and the
    legacy proxies must not double-count what they already covered."""

    def test_tool_reject_schema_counts_per_name(self, luxe):
        _run(luxe, "s1-0", [
            {"kind": "tool_reject", "reason": "schema", "name": "edit_file",
             "step": 2, "message": "Schema error: missing required key"},
            {"kind": "single_mode_done", "schema_rejects": 1},
        ])
        b, _ = _collect(luxe)
        assert b["schema_reject"].count == 1  # direct + remainder(0), not 2
        assert b["schema_reject"].by_key["edit_file"] == 1

    def test_legacy_run_total_still_counts(self, luxe):
        _run(luxe, "s1-0", [{"kind": "single_mode_done", "schema_rejects": 2}])
        b, _ = _collect(luxe)
        assert b["schema_reject"].count == 2

    def test_mixed_run_counts_only_the_remainder(self, luxe):
        _run(luxe, "s1-0", [
            {"kind": "tool_reject", "reason": "schema", "name": "edit_file",
             "step": 1, "message": "m"},
            {"kind": "single_mode_done", "schema_rejects": 3},
        ])
        b, _ = _collect(luxe)
        assert b["schema_reject"].count == 3  # 1 direct + 2 remainder

    def test_unknown_tool_direct_suppresses_the_name_heuristic(self, luxe):
        _run(luxe, "s1-0", [
            {"kind": "tool_reject", "reason": "unknown_tool",
             "name": "final_report", "step": 3,
             "message": "Unknown tool: final_report"},
            {"kind": "tool_call", "name": "final_report", "step": 3},
        ])
        b, _ = _collect(luxe)
        assert b["unknown_tool_name"].count == 1
        assert b["unknown_tool_name"].by_key["final_report"] == 1

    def test_textfallback_direct_event_counts_names(self, luxe):
        _run(luxe, "s1-0", [
            {"kind": "textfallback_drop", "names": ["made_up"], "step": 1,
             "recovered": False},
        ])
        b, _ = _collect(luxe)
        assert b["textfallback_drop"].count == 1
        assert b["textfallback_drop"].by_key["made_up"] == 1

    def test_textfallback_direct_suppresses_the_prose_proxy(self, luxe):
        _run(luxe, "s1-0", [
            {"kind": "textfallback_drop", "names": ["made_up"], "step": 1,
             "recovered": False},
        ])
        _session(luxe, "s1", [
            {"kind": "user", "text": "q"},
            {"kind": "assistant", "run_id": "s1-0",
             "text": '<tool_call>{"name": "made_up"}</tool_call>',
             "steps": 1, "tool_calls": 0},
        ])
        b, _ = _collect(luxe)
        assert b["textfallback_drop"].count == 1  # direct only, proxy quiet
