"""Unit tests for the goal-runner decision + test-output parser (C1/D1/D6).

`evaluate_goal_round` and `parse_test_result` are pure, so completion/stuck —
including the iter-5 observable-completion fix and the thrash detector — can be
verified without spinning a model.
"""

from luxe.chat.repl import evaluate_goal_round, parse_test_result


def _run(rounds):
    """Thread a sequence of round inputs through the decision helper. Each round
    is a dict: settled, sentinel, completed_count, new_completed, test_result.
    Returns the verdict that broke early (or the last 'continue')."""
    st = dict(done_streak=0, settled_no_progress=0, completed_ever_grew=False,
              thrash_count=0, best_failures=None, best_total=None)
    verdict = "continue"
    for r in rounds:
        d = evaluate_goal_round(
            settled=r["settled"], sentinel=r["sentinel"],
            completed_count=r["completed_count"], new_completed=r["new_completed"],
            test_result=r.get("test_result"), **st)
        st = dict(done_streak=d.done_streak, settled_no_progress=d.settled_no_progress,
                  completed_ever_grew=d.completed_ever_grew, thrash_count=d.thrash_count,
                  best_failures=d.best_failures, best_total=d.best_total)
        verdict = d.verdict
        if verdict in ("done", "stuck"):
            return verdict
    return verdict


# --- parse_test_result -----------------------------------------------------

def test_parse_pytest_summaries():
    assert parse_test_result("pytest", "72 passed, 4 failed in 2.1s", False) == (72, 4, 0)
    assert parse_test_result("pytest", "76 passed in 1.2s", False) == (76, 0, 0)
    assert parse_test_result("pytest", "1 passed, 1 error", False) == (1, 0, 1)
    # singular/plural + ordering tolerance
    assert parse_test_result("pytest", "4 failures, 1 passed", False) == (1, 4, 0)


def test_parse_crash_records_error():
    # test command crashed before a summary → errors=1 (non-progress, not ignored)
    assert parse_test_result("python -m pytest tests/", "Traceback ...\nSyntaxError",
                             True) == (0, 0, 1)


def test_parse_non_test_output_is_none():
    assert parse_test_result("ls -la", "a.py b.py", False) is None
    assert parse_test_result("", "wrote 500 bytes", False) is None


# --- completion paths ------------------------------------------------------

def test_green_tests_complete_without_sentinel_or_ledger():
    # The iter-5 run-2/3 fix: finished run, model never logged completed/sentinel,
    # but tests are green → completes via the observable path.
    verdict = _run([
        {"settled": False, "sentinel": False, "completed_count": 0,
         "new_completed": False, "test_result": (70, 6, 0)},  # building, failing
        {"settled": True, "sentinel": False, "completed_count": 0,
         "new_completed": False, "test_result": (76, 0, 0)},  # green
        {"settled": True, "sentinel": False, "completed_count": 0,
         "new_completed": False, "test_result": (76, 0, 0)},  # green again -> done
    ])
    assert verdict == "done"


def test_zero_test_project_does_not_auto_complete_via_green():
    # passed==0 must NOT count as green (a project with no tests).
    d = evaluate_goal_round(
        settled=True, sentinel=False, completed_count=0, new_completed=False,
        test_result=(0, 0, 0), done_streak=1, settled_no_progress=0,
        completed_ever_grew=False, thrash_count=0, best_failures=None, best_total=None)
    assert d.verdict != "done"


def test_sentinel_path_still_completes():
    verdict = _run([
        {"settled": False, "sentinel": False, "completed_count": 5,
         "new_completed": True, "test_result": None},
        {"settled": True, "sentinel": True, "completed_count": 5,
         "new_completed": False, "test_result": None},
        {"settled": True, "sentinel": True, "completed_count": 5,
         "new_completed": False, "test_result": None},
    ])
    assert verdict == "done"


# --- stuck guards ----------------------------------------------------------

def test_thrash_trips_on_flat_failures():
    # edit→test→same 4 failures, repeated, no new completed → thrash stuck.
    rounds = [{"settled": False, "sentinel": False, "completed_count": 0,
               "new_completed": False, "test_result": (72, 4, 0)} for _ in range(4)]
    assert _run(rounds) == "stuck"


def test_decreasing_failures_does_not_trip_thrash():
    verdict = _run([
        {"settled": False, "sentinel": False, "completed_count": 0,
         "new_completed": False, "test_result": (70, 6, 0)},
        {"settled": False, "sentinel": False, "completed_count": 0,
         "new_completed": False, "test_result": (72, 4, 0)},
        {"settled": False, "sentinel": False, "completed_count": 0,
         "new_completed": False, "test_result": (74, 2, 0)},
        {"settled": False, "sentinel": False, "completed_count": 0,
         "new_completed": False, "test_result": (75, 1, 0)},
    ])
    assert verdict == "continue"  # improving every round → never stuck


def test_no_test_work_rounds_do_not_trip_thrash():
    # Staged work (refactor/migrate) with no test run must not be punished.
    rounds = [{"settled": False, "sentinel": False, "completed_count": 0,
               "new_completed": False, "test_result": None} for _ in range(5)]
    assert _run(rounds) == "continue"


def test_idle_stuck_when_no_completed_and_no_tests():
    rounds = [{"settled": True, "sentinel": False, "completed_count": 0,
               "new_completed": False, "test_result": None} for _ in range(3)]
    assert _run(rounds) == "stuck"


def test_completion_beats_stuck_race():
    # done_streak==1, settled_no_progress==2, next round green/settled → DONE.
    d = evaluate_goal_round(
        settled=True, sentinel=False, completed_count=0, new_completed=False,
        test_result=(76, 0, 0), done_streak=1, settled_no_progress=2,
        completed_ever_grew=False, thrash_count=2, best_failures=4, best_total=76)
    assert d.verdict == "done"


def test_broken_build_no_sentinel_eventually_stuck():
    # Persistent errors, no green, no sentinel/completed → stuck (thrash), honest.
    rounds = [{"settled": False, "sentinel": False, "completed_count": 0,
               "new_completed": False, "test_result": (0, 0, 1)} for _ in range(4)]
    assert _run(rounds) == "stuck"
