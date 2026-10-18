"""Step-limit handling (luxe/steplimit.py, 2026-10-18).

tuning_2026_10: Qwen3.8 made its first edit at step 29 of 30, bound a method
it never wrote, and aborted at max steps. The half-applied change shipped
like a finished one and the bench printed PASS. Labelling (default ON) marks
such a run incomplete; the finish pass (opt-in) gets one bounded try at
completing it.
"""

from __future__ import annotations

from types import SimpleNamespace

from benchmarks.maintain_suite.run import _diagnose_step_limit
from luxe import pr as pr_mod
from luxe import steplimit
from luxe.pr import CmdResult, PRConfig
from luxe.run_state import PRState, RunSpec


def _res(aborted: bool, reason: str = ""):
    return SimpleNamespace(aborted=aborted, abort_reason=reason)


def test_hit_step_limit_main_and_reprompt():
    assert steplimit.hit_step_limit(_res(True, "Max steps reached (30)"))
    assert steplimit.hit_step_limit(_res(True, "reprompt: Max steps reached (30)"))
    assert not steplimit.hit_step_limit(_res(True, "stuck loop: 3 repeats"))
    assert not steplimit.hit_step_limit(_res(False, "Max steps reached (30)"))


def test_label_default_on_finish_default_off(monkeypatch):
    monkeypatch.delenv("LUXE_STEP_LIMIT_LABEL", raising=False)
    monkeypatch.delenv("LUXE_STEP_LIMIT_FINISH", raising=False)
    assert steplimit.label_enabled() and not steplimit.finish_enabled()
    monkeypatch.setenv("LUXE_STEP_LIMIT_LABEL", "0")
    monkeypatch.setenv("LUXE_STEP_LIMIT_FINISH", "1")
    assert not steplimit.label_enabled() and steplimit.finish_enabled()
    monkeypatch.setenv("LUXE_STEP_LIMIT_LABEL", "false")   # only "0" disables
    monkeypatch.setenv("LUXE_STEP_LIMIT_FINISH", "true")   # only "1" enables
    assert steplimit.label_enabled() and not steplimit.finish_enabled()


def test_label_report_is_idempotent_and_handles_empty():
    once = steplimit.label_report("Added Shift+R.")
    assert once.startswith(steplimit.REPORT_NOTE) and once.endswith("Added Shift+R.")
    assert steplimit.label_report(once) == once
    assert steplimit.label_report("") == steplimit.REPORT_NOTE


def test_finish_goal_carries_goal_and_diff_and_truncates():
    g = steplimit.finish_goal("Add Shift+R", "+this._onKeyDown.bind(this)")
    assert "Add Shift+R" in g and "_onKeyDown.bind" in g
    assert f"{steplimit.FINISH_MAX_STEPS} steps" in g
    big = steplimit.finish_goal("g", "x" * (steplimit.FINISH_DIFF_CHARS + 50))
    assert "[… diff truncated]" in big


def test_prstate_incomplete_round_trips():
    st = PRState(incomplete=True)
    assert PRState.from_dict(st.to_dict()).incomplete is True
    assert PRState.from_dict({}).incomplete is False   # old pr_state.json


def _spec(tmp_path) -> RunSpec:
    return RunSpec(run_id="t", goal="g", task_type="implement",
                   repo_path=str(tmp_path), base_branch="main")


def test_incomplete_pr_opens_as_draft_even_when_tests_pass(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(pr_mod, "_run_net", lambda cmd, cwd, **k: (
        calls.append(list(cmd)) or CmdResult(rc=0, stdout="https://x/pull/7", stderr="")))
    monkeypatch.setattr(pr_mod, "save_pr_state", lambda *a, **k: None)
    st = PRState(branch_name="b", test_passed=True, incomplete=True)
    pr_mod._do_create(_spec(tmp_path), st, "report", "implement", "g", PRConfig(test_commands=[]))
    assert "--draft" in calls[0] and st.is_draft is True


def test_complete_pr_with_passing_tests_is_not_draft(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(pr_mod, "_run_net", lambda cmd, cwd, **k: (
        calls.append(list(cmd)) or CmdResult(rc=0, stdout="https://x/pull/7", stderr="")))
    monkeypatch.setattr(pr_mod, "save_pr_state", lambda *a, **k: None)
    st = PRState(branch_name="b", test_passed=True)
    pr_mod._do_create(_spec(tmp_path), st, "report", "implement", "g", PRConfig(test_commands=[]))
    assert "--draft" not in calls[0]


def test_green_ci_never_promotes_an_incomplete_draft(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake(cmd, cwd, **k):
        calls.append(list(cmd))
        return CmdResult(rc=0, stdout="ci\tpass\t1m\turl\n", stderr="")

    monkeypatch.setattr(pr_mod, "_run_net", fake)
    monkeypatch.setattr(pr_mod.time, "sleep", lambda *_: None)
    cfg = PRConfig(test_commands=[], convert_to_ready_on_green=True)
    st = PRState(pr_number=7, is_draft=True, incomplete=True)
    pr_mod._do_watch_ci(_spec(tmp_path), st, cfg)
    assert st.is_draft is True
    assert not any(c[:3] == ["gh", "pr", "ready"] for c in calls)


def test_bench_warns_on_labelled_runs_only():
    assert _diagnose_step_limit(SimpleNamespace(events_kinds={})) == []
    out = _diagnose_step_limit(
        SimpleNamespace(events_kinds={"step_limit_incomplete": 1}))
    assert out and "hand-verify" in out[0]
