"""scripts/render_trajectory.py (harvest candidate 5)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "render_trajectory", Path(__file__).parents[1] / "scripts" / "render_trajectory.py")
rt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rt)

# Every kind seen across m5's 4538-run corpus on 2026-10-30, so a kind the
# renderer stops classifying fails here instead of becoming [UNKNOWN].
CORPUS_KINDS = """tool_call tool_step_done action_density_sample read_after_compact
step_usage pr_step_begin compaction_phase_reached pr_step_end
compaction_phase_at_resolve preflight_ok single_mode_done diff_stat pr_blocked
grep_after_compact compaction_phase_at_first_write early_bail_suppressed_diffuse
read_budget_applied citation_lint_passed post_write_idle_exit edit_after_compact
adaptive_state write_pressure_fired early_bail_breadth_probe_fired early_bail_fired
cleanup_turn truncated_turn_retry action_density_gate_fired
action_density_gate_suppressed_converged habituation_exit tool_reject reread_note
terminal_turn_truncated early_bail_suppressed_commit_only step_limit_incomplete
empty_turn_retry citation_lint_blocked step_limit_finish_begin
step_limit_finish_done textfallback_drop repeat_read_stub pr_unexpected_error
early_bail_suppressed_trajectory_shape""".split()


def _run(tmp_path: Path, events: list, report: str = "REPORT") -> Path:
    d = tmp_path / "run1"
    d.mkdir()
    (d / "run.json").write_text(json.dumps({"run_id": "run1", "goal": "do X",
                                            "task_type": "implement"}))
    (d / "events.jsonl").write_text("".join(
        (e if isinstance(e, str) else json.dumps(e)) + "\n" for e in events))
    (d / "synthesizer.md").write_text(report)
    return d


def test_corpus_has_42_kinds_all_classified():
    assert len(set(CORPUS_KINDS)) == 42
    for k in CORPUS_KINDS:
        line = rt.render_event({"kind": k, "phase": "main", "step": 1})
        assert line is None or not line.startswith("[UNKNOWN"), k


def test_grammar_and_footer(tmp_path):
    ev = [
        {"kind": "preflight_ok", "base_branch": "main"},
        {"kind": "tool_call", "phase": "main", "step": 0, "name": "read_file",
         "path": "a.py", "bytes_out": 10, "duplicate": True, "cached": False},
        {"kind": "action_density_sample", "phase": "main", "step": 0},
        {"kind": "step_usage", "phase": "main", "step": 0, "prompt_tokens": 5,
         "cached_tokens": 1, "completion_tokens": 2, "reasoning_chars": 3},
        {"kind": "compaction_phase_reached", "phase": "main", "step": 1, "phase_reached": 2,
         "tokens_before": 100, "tokens_after": 40, "tool_results_dropped": 3, "effective": True},
        {"kind": "write_pressure_fired", "phase": "main", "step": 1, "tool_calls_total": 9},
        {"kind": "brand_new_kind", "x": 1},
        "{not json",
    ]
    out = rt.render(_run(tmp_path, ev))
    assert "[STEP 0] main" in out and "[STEP 1] main" in out
    assert "[ACTION] read_file(a.py) [duplicate] -> 10 B" in out
    assert "[USAGE] prompt=5 cached=1 completion=2 reasoning_chars=3" in out
    assert "[COMPACT p2] tokens 100->40 dropped=3" in out
    assert "[INTERVENTION write_pressure_fired] tool_calls_total=9" in out
    assert "[RUN preflight_ok] base_branch=main" in out
    assert "[UNKNOWN brand_new_kind] x=1" in out
    assert "omitted telemetry: action_density_sample×1" in out
    assert "UNKNOWN kinds (renderer needs updating): brand_new_kind×1" in out
    assert "unparseable event lines: 1" in out
    assert "not recorded by luxe" in out
    assert out.index("## Final report") < out.index("## Footer")


def test_clipping_is_per_item_with_marker(tmp_path):
    out = rt.render(_run(tmp_path, [], report="x" * (rt.REPORT_CLIP + 50)))
    assert "…[+50 chars — render clip]" in out


def test_cell_mode_uses_state_and_diff(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rt.Path, "home", lambda: tmp_path)
    runs = tmp_path / ".luxe" / "runs"
    runs.mkdir(parents=True)
    _run(runs, [{"kind": "tool_call", "phase": "main", "step": 0, "name": "glob"}])
    cell = tmp_path / "cell"
    cell.mkdir()
    (cell / "state.json").write_text(json.dumps({"luxe_run_id": "run1"}))
    (cell / "diff.patch").write_text("+added line\n")
    assert rt.main(["--cell", str(cell)]) == 0
    out = capsys.readouterr().out
    assert "[ACTION] glob()" in out and "+added line" in out


def test_missing_run_is_exit_2(tmp_path, capsys):
    assert rt.main([str(tmp_path / "nope")]) == 2
