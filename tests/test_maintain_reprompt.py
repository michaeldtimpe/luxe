"""`maintain_pipeline`'s reprompt 2nd pass after its 2026-10-22 cleanup.

The reprompt wording moved from inline f-strings in `maintain.py` to
templates in `agents/prompts.py` (agents.sdd: the prompt registry module is
the single source of truth), and the twice-copied second-pass merge became
`_merge_pass`. Both are on the benchmark path, so the move must be
byte-identical. The reference functions below are the ORIGINAL f-strings,
copied verbatim from `maintain.py` at origin/main 3096dfe.
"""

from __future__ import annotations

import pytest

from luxe import maintain
from luxe.agents.loop import AgentResult
from luxe.agents.prompts import REPROMPT_SPEC_TEMPLATE
from luxe.tools.base import ToolCall


def _orig_spec_lead(goal):
    return f"You completed an initial pass on this goal:\n  {goal}\n\n"


def _orig_v1_3(goal, additions, deletions, diff_text, prior_text):
    if additions == 0 and len(prior_text) > 1000:
        followup_goal = (
            f"PROBLEM: You completed a pass on this goal but did NOT "
            f"call write_file or edit_file. The working tree has 0 "
            f"added lines. You produced extensive prose in your "
            f"final report but it is stranded — not saved to disk.\n\n"
            f"Original goal:\n  {goal}\n\n"
            f"Your prior final report (which you must now persist "
            f"to disk):\n\n{prior_text[:6000]}\n\n"
            f"Action: identify the file path the goal asks for "
            f"(e.g., 'CONFIG.md' for an env-var documentation task; "
            f"the path is named in the goal). Call write_file with "
            f"that path and a coherent document body derived from "
            f"the report above. Do this on your FIRST tool call. "
            f"Do not explore more files first. After write_file "
            f"succeeds, you may continue if the content needs "
            f"refinement."
        )
    else:
        followup_goal = (
            f"You completed an initial pass on this goal:\n  {goal}\n\n"
            f"The diff so far is small ({additions} added / "
            f"{deletions} deleted lines):\n"
            f"```diff\n{diff_text}\n```\n\n"
            f"Re-read the goal carefully. Identify each named deliverable. "
            f"For any deliverable NOT yet reflected in the diff, make the "
            f"missing edits now via edit_file or write_file. If you "
            f"believe the diff is complete, make no further edits and "
            f"explain in your response which lines satisfy each "
            f"deliverable."
        )
    return followup_goal


# Braces, format specs and newlines in every value: `.format` must insert
# them verbatim, exactly as the f-string did.
_GOAL = "Document {LUXE_X} in CONFIG.md — {0} {:>3} {{literal}}\nline 2"
_DIFF = "+def f():\n+    return {'a': 1}\n-x = '{y}'"
_LONG_PROSE = ("# Report {with} braces\n" + "z" * 7000)


@pytest.mark.parametrize("additions,deletions,diff_text,prior_text", [
    (0, 0, "", _LONG_PROSE),           # prose-mode: stranded report
    (0, 0, "", "x" * 1001),            # just over the prose threshold
    (0, 0, "", "x" * 1000),            # at it: small-diff branch
    (0, 2, _DIFF, ""),
    (3, 1, _DIFF, _LONG_PROSE),        # additions > 0 never takes prose
])
def test_v1_3_followup_goal_is_byte_identical(additions, deletions,
                                              diff_text, prior_text):
    got = maintain._v1_3_followup_goal(_GOAL, additions, deletions,
                                       diff_text, prior_text)
    assert got == _orig_v1_3(_GOAL, additions, deletions, diff_text,
                             prior_text)


def test_spec_lead_is_byte_identical():
    assert REPROMPT_SPEC_TEMPLATE.format(goal=_GOAL) == _orig_spec_lead(_GOAL)


def test_merge_pass_folds_counters_and_calls():
    main = AgentResult(tool_calls_total=3, schema_rejects=1, prompt_tokens=100,
                       completion_tokens=50, wall_s=1.5,
                       tool_calls=[ToolCall(id="a", name="read_file",
                                            arguments={})],
                       final_text="first", aborted=False)
    second = AgentResult(tool_calls_total=2, schema_rejects=0,
                         prompt_tokens=40, completion_tokens=20, wall_s=0.5,
                         tool_calls=[ToolCall(id="b", name="edit_file",
                                              arguments={})],
                         final_text="second", aborted=True,
                         abort_reason="x")
    maintain._merge_pass(main, second)
    assert (main.tool_calls_total, main.schema_rejects, main.prompt_tokens,
            main.completion_tokens, main.wall_s) == (5, 1, 140, 70, 2.0)
    assert [t.id for t in main.tool_calls] == ["a", "b"]
    # Only the six merged fields move; the callers handle the rest.
    assert (main.final_text, main.aborted, main.abort_reason) == (
        "first", False, "")
