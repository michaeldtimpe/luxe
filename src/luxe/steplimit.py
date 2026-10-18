"""Step-limit handling — a run that stops at max steps with a change on disk.

Found 2026-10-18 (acceptance/tuning_2026_10 REPORT §8): Qwen3.8 made its first
edit at step 29 of 30, bound a method it never got to write, and the run
aborted at max steps. The half-applied change was committed like any finished
one, `npm test` never constructed the handler, and the bench printed PASS on
code that throws on every new game. Across m5's run corpus, 111 runs ended at
max steps with a diff, about 4 of ~600 of them on the champion.

Two parts:
  - LABELLING (default ON, no model call): `maintain` marks the run
    incomplete. The report and commit body open with `REPORT_NOTE`, the PR
    opens as a draft and is never auto-promoted on green CI, and a
    `step_limit_incomplete` event lets the bench flag the result.
    `LUXE_STEP_LIMIT_LABEL=0` (exact string) disables it.
  - FINISH PASS (opt-in, `LUXE_STEP_LIMIT_FINISH=1`, unbenched): one bounded
    `run_single` that shows the model its diff and asks it to complete any
    half-applied edit. If that pass ends on its own, the run is no longer
    step-limited.
"""

from __future__ import annotations

import os
from typing import Any

from luxe.agents.prompts import STEP_LIMIT_FINISH_TEMPLATE

#: Steps the opt-in finish pass gets, and the diff excerpt it is shown.
FINISH_MAX_STEPS = 8
FINISH_DIFF_CHARS = 12_000

REPORT_NOTE = (
    "> **Incomplete: this run stopped at its step limit.** The change below "
    "is whatever was on disk when the budget ran out. It may be half-applied "
    "(a reference to code that was never written, an edit cut short), even "
    "if tests pass. Review it before merging."
)


def label_enabled() -> bool:
    """Default ON; only the exact string "0" disables."""
    return os.environ.get("LUXE_STEP_LIMIT_LABEL", "1") != "0"


def finish_enabled() -> bool:
    """Opt-in; only the exact string "1" enables."""
    return os.environ.get("LUXE_STEP_LIMIT_FINISH") == "1"


def hit_step_limit(result: Any) -> bool:
    """True when a SingleResult ended because it ran out of steps (main pass
    or reprompt — maintain prefixes the reprompt's reason)."""
    return bool(getattr(result, "aborted", False)) and (
        "max steps" in (getattr(result, "abort_reason", "") or "").lower())


def finish_goal(goal: str, diff_text: str, steps: int = FINISH_MAX_STEPS) -> str:
    diff = diff_text or ""
    if len(diff) > FINISH_DIFF_CHARS:
        diff = diff[:FINISH_DIFF_CHARS] + "\n[… diff truncated]"
    return STEP_LIMIT_FINISH_TEMPLATE.format(goal=goal, diff=diff, steps=steps)


def label_report(report: str) -> str:
    """`report` with the incomplete note on top; idempotent."""
    report = report or ""
    if report.startswith(REPORT_NOTE):
        return report
    return f"{REPORT_NOTE}\n\n{report}" if report else REPORT_NOTE
