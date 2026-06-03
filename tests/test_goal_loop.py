"""Unit tests for the goal-runner per-round decision (C1).

`evaluate_goal_round` is a pure function so the completion/stuck logic — and
especially the completion-beats-STUCK ordering that the iter-4 `a5812` false-STUCK
exposed — can be verified without spinning a model.
"""

from luxe.chat.repl import evaluate_goal_round


def _run(rounds):
    """Drive a sequence of round inputs through the helper, threading state.
    Each round is a dict with keys: settled, sentinel, completed_count,
    new_completed. Returns the final verdict (or the verdict that broke early)."""
    done_streak = 0
    settled_no_progress = 0
    grew = False
    verdict = "continue"
    for r in rounds:
        d = evaluate_goal_round(
            settled=r["settled"], sentinel=r["sentinel"],
            completed_count=r["completed_count"], new_completed=r["new_completed"],
            done_streak=done_streak, settled_no_progress=settled_no_progress,
            completed_ever_grew=grew)
        done_streak = d.done_streak
        settled_no_progress = d.settled_no_progress
        grew = d.completed_ever_grew
        verdict = d.verdict
        if verdict in ("done", "stuck"):
            break
    return verdict


def test_a5812_replay_completes_not_stuck():
    # Work done (completed grew to 5), then 2 settled+sentinel rounds. A lingering
    # in_progress item is irrelevant — the helper never consults it.
    verdict = _run([
        {"settled": False, "sentinel": False, "completed_count": 5, "new_completed": True},
        {"settled": True, "sentinel": True, "completed_count": 5, "new_completed": False},
        {"settled": True, "sentinel": True, "completed_count": 5, "new_completed": False},
    ])
    assert verdict == "done"


def test_genuine_stuck_when_no_completed_work():
    # 32K-style failure: settled rounds, no sentinel, completed never grows.
    verdict = _run([
        {"settled": True, "sentinel": False, "completed_count": 0, "new_completed": False},
        {"settled": True, "sentinel": False, "completed_count": 0, "new_completed": False},
        {"settled": True, "sentinel": False, "completed_count": 0, "new_completed": False},
    ])
    assert verdict == "stuck"


def test_completion_beats_stuck_race():
    # done_streak==1 and settled_no_progress==2 already; the next round is
    # settled + sentinel + completed → must COMPLETE, not STUCK.
    d = evaluate_goal_round(
        settled=True, sentinel=True, completed_count=4, new_completed=False,
        done_streak=1, settled_no_progress=2, completed_ever_grew=True)
    assert d.verdict == "done"


def test_sentinel_required_idle_does_not_complete():
    # Settled + completed work + grew, but NO sentinel (idle path dropped): must
    # not certify done; with no new completed it accrues toward stuck instead.
    verdict = _run([
        {"settled": False, "sentinel": False, "completed_count": 3, "new_completed": True},
        {"settled": True, "sentinel": False, "completed_count": 3, "new_completed": False},
        {"settled": True, "sentinel": False, "completed_count": 3, "new_completed": False},
        {"settled": True, "sentinel": False, "completed_count": 3, "new_completed": False},
    ])
    assert verdict == "stuck"


def test_prepopulated_ledger_does_not_auto_complete():
    # completed_count>0 from the start but never grows → completed_ever_grew stays
    # False → never corroborates even with the sentinel.
    d = evaluate_goal_round(
        settled=True, sentinel=True, completed_count=9, new_completed=False,
        done_streak=1, settled_no_progress=0, completed_ever_grew=False)
    assert d.verdict != "done"


def test_corroborated_round_resets_stuck_counter():
    d = evaluate_goal_round(
        settled=True, sentinel=True, completed_count=2, new_completed=True,
        done_streak=0, settled_no_progress=2, completed_ever_grew=False)
    assert d.settled_no_progress == 0
    assert d.verdict == "continue"  # only 1 corroborated round so far
