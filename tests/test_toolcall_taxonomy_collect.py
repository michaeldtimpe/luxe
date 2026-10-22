"""`toolcall_taxonomy.collect` — characterization of every branch it takes.

`tests/test_toolcall_taxonomy.py` pins the headline counting rules. These
pin the rest of `collect`'s behaviour — window edges, per-run scoping of the
direct-event suppression, the session turn counter, malformed input, stats
keys — so the function can be decomposed without moving a single number in
`acceptance/toolcall_taxonomy_2026_08/`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import toolcall_taxonomy as tt  # noqa: E402

NOW = time.time()
CUTOFF = NOW - 1000.0
OLD = NOW - 2000.0          # outside the window
NEW = NOW - 10.0            # inside it


@pytest.fixture
def luxe(tmp_path: Path) -> Path:
    return tmp_path / ".luxe"


def _lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(ln + "\n" for ln in lines))


def _run(luxe: Path, run_id: str, recs: list[dict]) -> None:
    for r in recs:
        r.setdefault("ts", NEW)
    _lines(luxe / "runs" / run_id / "events.jsonl", [json.dumps(r) for r in recs])


def _session(luxe: Path, sid: str, recs: list[dict]) -> None:
    for r in recs:
        r.setdefault("ts", NEW)
    _lines(luxe / "sessions" / sid / "transcript.jsonl",
           [json.dumps(r) for r in recs])


def _log(luxe: Path, sid: str, body: str) -> Path:
    path = luxe / "sessions" / sid / "debug.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _collect(luxe: Path, **kw):
    return tt.collect(luxe, CUTOFF, **kw)


def _counts(b: dict) -> dict:
    return {k: v.count for k, v in b.items() if v.count}


class TestEmptyAndMalformed:
    def test_missing_roots_give_empty_buckets_and_only_the_fixed_stats(self, luxe):
        luxe.mkdir()
        b, stats = _collect(luxe)
        assert list(b) == ["schema_reject", "unknown_tool_name",
                           "duplicate_storm", "textfallback_drop",
                           "empty_response", "aborted_run", "turn_error",
                           "backend_retry"]
        assert _counts(b) == {}
        tools, tools_from = tt.known_tools()
        assert dict(stats) == {
            "_tools_known": len(tools),
            "_meta": {"tools_from": tools_from,
                      "apparatus": "ccab-, smoke-, test-, capdrill-"},
        }
        assert not b["backend_retry"].measurable
        assert "no retry decision lines" in b["backend_retry"].why_unmeasurable
        assert all(v.measurable for k, v in b.items() if k != "backend_retry")

    def test_include_apparatus_is_named_in_meta(self, luxe):
        luxe.mkdir()
        _, stats = _collect(luxe, include_apparatus=True)
        assert stats["_meta"]["apparatus"] == "included"

    def test_run_dir_without_events_and_stray_files_are_ignored(self, luxe):
        (luxe / "runs" / "s1-0").mkdir(parents=True)
        (luxe / "runs" / "loose.jsonl").write_text("{}\n")
        (luxe / "runs" / "ccab-nofile").mkdir()
        (luxe / "sessions").mkdir()
        (luxe / "sessions" / "loose.txt").write_text("x")
        b, stats = _collect(luxe)
        assert _counts(b) == {}
        assert stats["runs_in_window"] == 0
        # The apparatus skip comes AFTER the events-file check.
        assert stats["runs_skipped_apparatus"] == 0

    def test_blank_and_malformed_lines_are_skipped(self, luxe):
        _lines(luxe / "runs" / "s1-0" / "events.jsonl", [
            "", "   ", "{not json",
            json.dumps({"kind": "tool_call", "name": "final_report", "ts": NEW}),
            "[1, 2",
        ])
        _lines(luxe / "sessions" / "s1" / "transcript.jsonl", [
            "garbage", json.dumps({"kind": "error", "text": "E: x", "ts": NEW}),
        ])
        b, stats = _collect(luxe)
        assert _counts(b) == {"unknown_tool_name": 1, "turn_error": 1}
        assert stats["tool_calls"] == 1

    def test_a_record_without_ts_is_outside_any_real_window(self, luxe):
        _lines(luxe / "runs" / "s1-0" / "events.jsonl",
               [json.dumps({"kind": "tool_call", "name": "final_report"}),
                json.dumps({"kind": "tool_call", "name": "nope", "ts": None})])
        b, stats = _collect(luxe)
        assert _counts(b) == {}
        assert stats["runs_in_window"] == 0
        assert "tool_calls" not in stats


class TestRunWindow:
    def test_only_in_window_records_count_and_mark_the_run(self, luxe):
        _run(luxe, "s1-0", [
            {"kind": "tool_call", "name": "final_report", "ts": OLD},
            {"kind": "tool_call", "name": "read_file", "duplicate": True,
             "ts": OLD},
            {"kind": "tool_call", "name": "read_file"},
        ])
        _run(luxe, "s2-0", [{"kind": "tool_call", "name": "x", "ts": OLD}])
        b, stats = _collect(luxe)
        assert _counts(b) == {}
        assert stats["runs_in_window"] == 1
        assert stats["tool_calls"] == 1
        assert stats["duplicate_calls"] == 0

    def test_a_fully_out_of_window_run_adds_no_stats_keys(self, luxe):
        _run(luxe, "s1-0", [{"kind": "single_mode_done", "aborted": True,
                             "ts": OLD}])
        _, stats = _collect(luxe)
        for key in ("runs_in_window", "duplicate_calls", "runs_completed",
                    "tool_calls"):
            assert key not in stats

    @pytest.mark.parametrize("prefix", tt.APPARATUS_PREFIXES)
    def test_every_apparatus_prefix_is_skipped(self, luxe, prefix):
        _run(luxe, f"{prefix}x", [{"kind": "tool_call", "name": "foo"}])
        b, stats = _collect(luxe)
        assert stats["runs_skipped_apparatus"] == 1
        assert "tool_calls" not in stats and _counts(b) == {}


class TestDuplicates:
    def test_duplicate_calls_accumulate_below_the_storm_threshold(self, luxe):
        _run(luxe, "s1-0", [{"kind": "tool_call", "name": "read_file",
                             "duplicate": True}] * 2)
        _run(luxe, "s2-0", [{"kind": "tool_call", "name": "read_file",
                             "duplicate": True}] * 3)
        b, stats = _collect(luxe)
        assert stats["duplicate_calls"] == 5
        assert b["duplicate_storm"].count == 1
        assert b["duplicate_storm"].examples == ["s2-0: 3 duplicates"]
        assert b["duplicate_storm"].by_key == {"3": 1}

    def test_a_run_with_no_duplicates_still_writes_the_stat(self, luxe):
        _run(luxe, "s1-0", [{"kind": "tool_call", "name": "read_file"}])
        _, stats = _collect(luxe)
        assert "duplicate_calls" in stats and stats["duplicate_calls"] == 0


class TestDirectToolReject:
    def test_schema_example_key_and_message_truncation(self, luxe):
        _run(luxe, "s1-0", [{"kind": "tool_reject", "reason": "schema",
                             "name": "  edit_file ", "step": 4,
                             "message": "M" * 100}])
        b, _ = _collect(luxe)
        sr = b["schema_reject"]
        assert sr.examples == [f"s1-0 step 4: 'edit_file': {'M' * 60} (direct)"]
        assert sr.by_key == {"edit_file": 1} and sr.sessions == {"s1"}

    def test_unnamed_reject_and_missing_message(self, luxe):
        _run(luxe, "s1-0", [
            {"kind": "tool_reject", "reason": "schema", "name": "  "},
            {"kind": "tool_reject", "reason": "unknown_tool"},
        ])
        b, _ = _collect(luxe)
        assert b["schema_reject"].examples == [
            "s1-0 step None: '(unnamed)':  (direct)"]
        assert b["unknown_tool_name"].examples == [
            "s1-0 step None: '(unnamed)' (direct)"]

    def test_an_unrecognised_reason_counts_nowhere(self, luxe):
        _run(luxe, "s1-0", [{"kind": "tool_reject", "reason": "other",
                             "name": "x"},
                            {"kind": "tool_reject", "name": "y"}])
        b, _ = _collect(luxe)
        assert _counts(b) == {}

    def test_direct_schema_is_scoped_to_its_own_run(self, luxe):
        # Symmetric runs, so a leak shows whatever order scandir yields:
        # each is 1 direct + 1 remainder; a carried-over direct count would
        # eat the second run's remainder.
        for sid in ("s1", "s2"):
            _run(luxe, f"{sid}-0", [
                {"kind": "tool_reject", "reason": "schema",
                 "name": "edit_file", "step": 1, "message": "m"},
                {"kind": "single_mode_done", "schema_rejects": 2}])
        b, _ = _collect(luxe)
        assert b["schema_reject"].count == 4
        assert b["schema_reject"].sessions == {"s1", "s2"}

    def test_direct_beyond_the_run_total_never_goes_negative(self, luxe):
        _run(luxe, "s1-0", [
            {"kind": "tool_reject", "reason": "schema", "name": "a", "step": 1},
            {"kind": "tool_reject", "reason": "schema", "name": "b", "step": 2},
            {"kind": "single_mode_done", "schema_rejects": 1},
        ])
        b, stats = _collect(luxe)
        assert b["schema_reject"].count == 2
        assert stats["runs_completed"] == 1

    def test_legacy_remainder_example_names_the_remainder(self, luxe):
        _run(luxe, "s1-0", [
            {"kind": "tool_reject", "reason": "schema", "name": "a", "step": 1},
            {"kind": "single_mode_done", "schema_rejects": 3},
        ])
        b, _ = _collect(luxe)
        assert b["schema_reject"].examples[1:] == ["s1-0: 2 reject(s)"] * 2
        assert b["schema_reject"].by_key == {"a": 1}

    def test_direct_after_done_is_not_subtracted(self, luxe):
        """Suppression is order-dependent: only rejects seen BEFORE the
        run's `single_mode_done` come off its legacy total."""
        _run(luxe, "s1-0", [
            {"kind": "single_mode_done", "schema_rejects": 1},
            {"kind": "tool_reject", "reason": "schema", "name": "a", "step": 1},
        ])
        b, _ = _collect(luxe)
        assert b["schema_reject"].count == 2


class TestUnknownToolSuppression:
    def test_direct_suppresses_only_the_same_step_and_name(self, luxe):
        _run(luxe, "s1-0", [
            {"kind": "tool_call", "name": "final_report", "step": 3},
            {"kind": "tool_reject", "reason": "unknown_tool",
             "name": "final_report", "step": 3},
            {"kind": "tool_call", "name": "final_report", "step": 4},
            {"kind": "tool_call", "name": "other_fake", "step": 3},
        ])
        b, _ = _collect(luxe)
        ut = b["unknown_tool_name"]
        # Direct hits land during the scan, the proxy remainder after it.
        assert ut.examples == [
            "s1-0 step 3: 'final_report' (direct)",
            "s1-0 step 4: 'final_report'",
            "s1-0 step 3: 'other_fake'",
        ]
        assert ut.by_key == {"final_report": 2, "other_fake": 1}

    def test_direct_unknown_is_scoped_to_its_own_run(self, luxe):
        # Each run's direct event names the OTHER run's proxy call, so a
        # carried-over set suppresses whichever run scandir yields second.
        for sid, direct, proxied in (("s1", "fake_a", "fake_b"),
                                     ("s2", "fake_b", "fake_a")):
            _run(luxe, f"{sid}-0", [
                {"kind": "tool_reject", "reason": "unknown_tool",
                 "name": direct, "step": 1},
                {"kind": "tool_call", "name": proxied, "step": 1}])
        b, _ = _collect(luxe)
        assert b["unknown_tool_name"].count == 4

    def test_an_empty_name_is_not_a_hallucination(self, luxe):
        _run(luxe, "s1-0", [{"kind": "tool_call", "name": "  "},
                            {"kind": "tool_call"}])
        b, stats = _collect(luxe)
        assert _counts(b) == {} and stats["tool_calls"] == 2

    def test_a_non_chat_run_id_is_its_own_session(self, luxe):
        _run(luxe, "bench-abc", [{"kind": "tool_call", "name": "fake"}])
        _run(luxe, "bench-abc-7", [{"kind": "tool_call", "name": "fake"}])
        b, _ = _collect(luxe)
        assert b["unknown_tool_name"].sessions == {"bench-abc"}
        assert b["unknown_tool_name"].count == 2


class TestDirectTextfallbackDrop:
    def test_each_dropped_name_is_one_hit(self, luxe):
        _run(luxe, "s1-0", [
            {"kind": "textfallback_drop", "names": ["a", 7], "step": 2},
            {"kind": "textfallback_drop", "names": [], "step": 3},
            {"kind": "textfallback_drop", "step": 4},
        ])
        b, _ = _collect(luxe)
        tf = b["textfallback_drop"]
        assert tf.examples == [
            "s1-0 step 2: 'a' (direct)", "s1-0 step 2: '7' (direct)",
            "s1-0 step 3: '(unnamed)' (direct)",
            "s1-0 step 4: '(unnamed)' (direct)",
        ]
        assert tf.by_key == {"a": 1, "7": 1, "(unnamed)": 2}

    def test_an_in_window_direct_drop_suppresses_the_proxy(self, luxe):
        _run(luxe, "s1-0", [{"kind": "textfallback_drop", "names": ["a"]}])
        _session(luxe, "s1", [{"kind": "user"},
                              {"kind": "assistant", "run_id": "s1-0",
                               "text": "<tool_call>x"}])
        b, _ = _collect(luxe)
        assert b["textfallback_drop"].examples == ["s1-0 step None: 'a' (direct)"]

    def test_an_out_of_window_direct_drop_does_not_suppress_the_proxy(self, luxe):
        _run(luxe, "s1-0", [{"kind": "textfallback_drop", "names": ["a"],
                             "ts": OLD}])
        _session(luxe, "s1", [{"kind": "user"},
                              {"kind": "assistant", "run_id": "s1-0",
                               "text": "<tool_call>x"}])
        b, _ = _collect(luxe)
        assert b["textfallback_drop"].examples == [
            "s1-0: <tool_call> in prose, 0 tool events"]


class TestAborts:
    def test_abort_reason_defaults_and_non_aborts(self, luxe):
        _run(luxe, "s1-0", [{"kind": "single_mode_done", "aborted": True}])
        _run(luxe, "s2-0", [{"kind": "single_mode_done", "aborted": False,
                             "abort_reason": "ignored"}])
        b, stats = _collect(luxe)
        assert b["aborted_run"].examples == ["s1-0: (unreported)"]
        assert b["aborted_run"].by_key == {"(unreported)": 1}
        assert stats["runs_completed"] == 2


class TestSessionTurns:
    def test_out_of_window_user_records_still_advance_the_turn(self, luxe):
        _run(luxe, "s1-0", [{"kind": "tool_call", "name": "read_file"}])
        _session(luxe, "s1", [
            {"kind": "user", "ts": OLD},
            {"kind": "user"},
            {"kind": "assistant", "text": "<tool_call>{}</tool_call>"},
        ])
        b, _ = _collect(luxe)
        assert b["textfallback_drop"].examples == [
            "s1-1: <tool_call> in prose, 0 tool events"]

    def test_an_assistant_before_any_user_is_turn_zero(self, luxe):
        _session(luxe, "s1", [{"kind": "assistant", "text": "",
                               "steps": 2, "tool_calls": 0}])
        b, stats = _collect(luxe)
        assert b["empty_response"].examples == ["s1-0: steps=2 tools=0"]
        assert stats["assistant_turns"] == 1

    def test_a_proxy_drop_counts_when_the_runs_calls_were_out_of_window(
            self, luxe):
        _run(luxe, "s1-0", [{"kind": "tool_call", "name": "read_file",
                             "ts": OLD}])
        _session(luxe, "s1", [{"kind": "user"},
                              {"kind": "assistant", "run_id": "s1-0",
                               "text": "<tool_call>"}])
        b, _ = _collect(luxe)
        assert b["textfallback_drop"].count == 1

    def test_out_of_window_assistant_and_error_records_are_ignored(self, luxe):
        _session(luxe, "s1", [
            {"kind": "user"},
            {"kind": "assistant", "text": "", "ts": OLD},
            {"kind": "error", "text": "E: x", "ts": OLD},
        ])
        b, stats = _collect(luxe)
        assert _counts(b) == {}
        assert "assistant_turns" not in stats and "error_records" not in stats

    def test_empty_response_whitespace_and_missing_text(self, luxe):
        _session(luxe, "s1", [
            {"kind": "assistant", "text": "  \n"},
            {"kind": "assistant"},
            {"kind": "assistant", "text": None, "interrupted": True},
            {"kind": "assistant", "text": "fine"},
        ])
        b, stats = _collect(luxe)
        assert b["empty_response"].examples == [
            "s1-0: steps=None tools=None"] * 2
        assert stats["assistant_turns"] == 4

    def test_an_interrupted_turn_still_feeds_the_prose_proxy(self, luxe):
        _session(luxe, "s1", [{"kind": "assistant", "interrupted": True,
                               "text": "<tool_call>"}])
        b, _ = _collect(luxe)
        assert _counts(b) == {"textfallback_drop": 1}


class TestTurnErrors:
    def test_keys_examples_and_truncation(self, luxe):
        _session(luxe, "s1", [
            {"kind": "error", "text": "  "},
            {"kind": "error"},
            {"kind": "error", "text": "K" * 50 + ": " + "x" * 200},
            {"kind": "error", "text": ": leading colon"},
        ])
        b, stats = _collect(luxe)
        te = b["turn_error"]
        assert stats["error_records"] == 4
        assert te.by_key == {"(bare)": 3, "K" * 40: 1}
        assert te.examples[:2] == ["s1: ", "s1: "]
        assert te.examples[2] == "s1: " + ("K" * 50 + ": " + "x" * 200)[:120]


class TestDebugLog:
    @pytest.mark.parametrize("token", [
        "transient-timeout", "5xx-transient-502", "5xx-terminal-500",
        "5xx-empty-body", "4xx-429", "unknown-error-foo",
        "exhausted-attempts", "unexpected-3", "200-error-body",
        "200-malformed-body", "stream-error-chunk", "progress-stall",
    ])
    def test_each_retry_class_is_a_keyed_hit(self, luxe, token):
        _log(luxe, "s1", f"WARNING decision={token} attempt=1\n")
        b, stats = _collect(luxe)
        br = b["backend_retry"]
        assert br.measurable and br.why_unmeasurable == ""
        assert br.by_key == {token: 1}
        assert br.examples == [f"s1: {token}"]
        assert stats["debug_log_lines"] == 1

    def test_lines_without_decisions_leave_it_unmeasurable(self, luxe):
        _log(luxe, "s1", "a\nb\nc")
        b, stats = _collect(luxe)
        assert stats["debug_log_lines"] == 2
        assert not b["backend_retry"].measurable

    def test_matches_without_a_newline_still_leave_it_unmeasurable(self, luxe):
        _log(luxe, "s1", "decision=progress-stall")
        b, stats = _collect(luxe)
        assert b["backend_retry"].count == 1
        assert stats["debug_log_lines"] == 0
        assert not b["backend_retry"].measurable

    def test_the_log_is_not_windowed_and_needs_no_transcript(self, luxe):
        path = _log(luxe, "s1", "x progress-stall\ny 4xx-400\n")
        os.utime(path, (OLD - 1e6, OLD - 1e6))
        b, _ = _collect(luxe)
        assert b["backend_retry"].count == 2
        assert b["backend_retry"].sessions == {"s1"}

    def test_an_unreadable_log_counts_as_empty(self, luxe):
        path = _log(luxe, "s1", "x progress-stall\n")
        path.chmod(0)
        try:
            if os.access(path, os.R_OK):
                pytest.skip("running with privileges that ignore chmod 0")
            b, stats = _collect(luxe)
        finally:
            path.chmod(0o644)
        assert stats["debug_log_lines"] == 0
        assert b["backend_retry"].count == 0

    def test_a_debug_log_directory_is_skipped(self, luxe):
        (luxe / "sessions" / "s1" / "debug.log").mkdir(parents=True)
        b, stats = _collect(luxe)
        assert stats["debug_log_lines"] == 0 and _counts(b) == {}


class TestWholeCorpus:
    def test_every_stat_and_bucket_on_a_mixed_corpus(self, luxe):
        """One corpus touching every source at once — the stats dict must
        carry exactly these keys (Counter keys appear on first `+=`)."""
        _run(luxe, "s1-0", [
            {"kind": "tool_call", "name": "read_file", "duplicate": True},
            {"kind": "tool_call", "name": "read_file", "duplicate": True},
            {"kind": "tool_call", "name": "read_file", "duplicate": True},
            {"kind": "tool_call", "name": "fake", "step": 5},
            {"kind": "tool_reject", "reason": "schema", "name": "edit_file",
             "step": 6, "message": "bad"},
            {"kind": "single_mode_done", "schema_rejects": 2, "aborted": True,
             "abort_reason": "Max steps reached (30)"},
        ])
        _run(luxe, "smoke-1", [{"kind": "tool_call", "name": "x"}])
        _session(luxe, "s1", [
            {"kind": "user"},
            {"kind": "assistant", "run_id": "s1-0", "text": "<tool_call>"},
            {"kind": "user"},
            {"kind": "assistant", "text": ""},
            {"kind": "error", "text": "BackendError: boom"},
        ])
        _log(luxe, "s1", "decision=transient-timeout\n")
        b, stats = _collect(luxe)
        tools, tools_from = tt.known_tools()
        assert dict(stats) == {
            "_tools_known": len(tools),
            "runs_skipped_apparatus": 1,
            "tool_calls": 4,
            "runs_completed": 1,
            "runs_in_window": 1,
            "duplicate_calls": 3,
            "assistant_turns": 2,
            "error_records": 1,
            "debug_log_lines": 1,
            "_meta": {"tools_from": tools_from,
                      "apparatus": "ccab-, smoke-, test-, capdrill-"},
        }
        assert _counts(b) == {"schema_reject": 2, "unknown_tool_name": 1,
                              "duplicate_storm": 1, "empty_response": 1,
                              "aborted_run": 1, "turn_error": 1,
                              "backend_retry": 1}
        assert b["schema_reject"].examples == [
            "s1-0 step 6: 'edit_file': bad (direct)", "s1-0: 1 reject(s)"]
        assert b["empty_response"].examples == ["s1-1: steps=None tools=None"]
        assert all(v.sessions == {"s1"} for v in b.values() if v.count)
        assert b["backend_retry"].measurable
