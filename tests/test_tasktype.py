"""infer_task_type matches keywords on word boundaries (2026-09 review)."""

import pytest

from luxe.agents.tasktype import infer_task_type


@pytest.mark.parametrize("goal,expected", [
    # Substring false positives the old heuristic had:
    ("Summarize the error report", "summarize"),      # "port"
    ("Review the important module", "review"),       # "port"
    ("Explain the specific behaviour", "summarize"),  # "ci"
    ("update deps", "manage"),                        # "update" won
    # Inflections still route:
    ("Add a retry to the client", "implement"),
    ("creating a helper", "implement"),
    ("Removed the flag", "implement"),
    ("fixes the crash", "bugfix"),
    ("handling of errors", "bugfix"),
    ("write docs for x", "document"),
    ("bump the docker image", "manage"),
    ("look around", "review"),
])
def test_infer_task_type(goal, expected):
    assert infer_task_type(goal) == expected
