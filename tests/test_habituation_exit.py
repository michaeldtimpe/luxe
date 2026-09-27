"""HabituationExitGuard must not exit on the step its latest intervention fired.

The exit check runs after the step's nudge guards. When the third distinct
intervention fired at step >= 20, the run used to exit on that same step, so
the nudge was appended and never sent (4 of 46 historical habituation exits,
all matplotlib-25775, had since_last_intervention=0).
"""

from __future__ import annotations

from luxe.agents.guardrails import (
    _HABITUATION_EXIT_MIN_KINDS,
    _HABITUATION_EXIT_MIN_STEP,
    HabituationExitGuard,
)

KINDS = {f"kind{i}" for i in range(_HABITUATION_EXIT_MIN_KINDS)}
STEP = _HABITUATION_EXIT_MIN_STEP + 2


def _exit(step: int, last: int | None):
    return HabituationExitGuard.should_exit(
        intervention_kinds_fired=KINDS,
        first_write_step_after_intervention=None,
        step=step,
        last_intervention_step=last,
        tool_calls_total=30,
        completion_tokens=9000,
    )


def test_no_exit_on_the_step_the_latest_intervention_fired():
    assert _exit(STEP, last=STEP) is None


def test_exits_once_the_model_has_answered_the_intervention():
    out = _exit(STEP + 1, last=STEP)
    assert out is not None
    assert out["since_last_intervention"] == 1


def test_older_intervention_still_exits():
    out = _exit(STEP, last=STEP - 5)
    assert out is not None and out["since_last_intervention"] == 5


def test_no_recorded_intervention_step_keeps_prior_behavior():
    assert _exit(STEP, last=None) is not None
