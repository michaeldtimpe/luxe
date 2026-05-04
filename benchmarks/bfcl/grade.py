"""BFCL response grader — function name match + args allowed-set check.

PRELIMINARY (2026-05-03). Implements a simplified subset of BFCL's
official grader sufficient for the Python categories we target (simple,
multiple, parallel, parallel_multiple, irrelevance). Multi-turn grading
is more involved (state tracking) and will be added incrementally.

Ground-truth shape per BFCL v4 (`possible_answer/<category>.json`):
    {"id": "...",
     "ground_truth": [
        {"<func_name>": {"<arg_name>": [acceptable_values...]}}
     ]}

A response passes if:
- `simple` / `multiple`: model emits ONE tool call whose name matches
  the gt function and whose arg values are each in the gt list.
- `parallel` / `parallel_multiple`: model emits MULTIPLE tool calls;
  each must match the corresponding gt entry (order-insensitive).
- `irrelevance`: the model must NOT call any tool. (No gt; pass = no
  tool calls.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class GradeResult:
    passed: bool
    reason: str
    expected_calls: int = 0
    actual_calls: int = 0


def _value_matches(actual: Any, allowed_list: list[Any]) -> bool:
    """A model's emitted arg value matches if it's `==` to any element of
    allowed_list. Strings are compared case-sensitively; numerics by
    value (so `5 == 5.0` passes if either is in the allowed list).
    """
    for allowed in allowed_list:
        if actual == allowed:
            return True
        # Handle str↔number ambiguity: BFCL ground truth occasionally
        # lists numeric values as strings ("5") when the spec is integer.
        if isinstance(actual, (int, float)) and isinstance(allowed, str):
            try:
                if actual == float(allowed):
                    return True
            except ValueError:
                pass
        if isinstance(actual, str) and isinstance(allowed, (int, float)):
            try:
                if float(actual) == allowed:
                    return True
            except ValueError:
                pass
    return False


def _call_matches_gt_entry(
    call_name: str,
    call_args: dict[str, Any],
    gt_entry: dict[str, dict[str, list[Any]]],
) -> bool:
    """Return True iff call_name + call_args match this GT entry.

    A match requires:
    - `call_name == sole key in gt_entry`
    - For each arg in gt_entry's value: call_args[arg] is in the allowed
      list. Optional args (whose allowed list contains `""` or default
      sentinels) are tolerated whether emitted or not.
    """
    if len(gt_entry) != 1:
        return False
    gt_name = next(iter(gt_entry))
    if call_name != gt_name:
        return False
    gt_args: dict[str, list[Any]] = gt_entry[gt_name]
    for arg_name, allowed in gt_args.items():
        if arg_name not in call_args:
            # Optional arg — the GT lists "" or a sentinel as accepted.
            if "" in allowed or None in allowed:
                continue
            return False
        if not _value_matches(call_args[arg_name], allowed):
            return False
    # Reject if model passed extra args not in GT (BFCL is strict about
    # superfluous args for some categories — be lenient here, just warn
    # via reason field upstream if you need to).
    return True


def grade_simple(
    actual_calls: list[tuple[str, dict[str, Any]]],
    ground_truth: list[dict[str, dict[str, list[Any]]]],
) -> GradeResult:
    """Grade a `simple` or `multiple` problem — one tool call expected,
    any GT entry can be the right answer.
    """
    if len(actual_calls) == 0:
        return GradeResult(False, "no_tool_call_emitted",
                           expected_calls=1, actual_calls=0)
    if len(actual_calls) > 1:
        return GradeResult(False, f"emitted_{len(actual_calls)}_calls_expected_1",
                           expected_calls=1, actual_calls=len(actual_calls))

    call_name, call_args = actual_calls[0]
    for gt_entry in ground_truth:
        if _call_matches_gt_entry(call_name, call_args, gt_entry):
            return GradeResult(True, "matched_gt_entry",
                               expected_calls=1, actual_calls=1)
    return GradeResult(False, f"call_{call_name}_did_not_match_any_gt",
                       expected_calls=1, actual_calls=1)


def grade_parallel(
    actual_calls: list[tuple[str, dict[str, Any]]],
    ground_truth: list[dict[str, dict[str, list[Any]]]],
) -> GradeResult:
    """Grade `parallel` / `parallel_multiple` — multiple tool calls
    expected, set-equivalence with GT (order-insensitive).
    """
    expected = len(ground_truth)
    actual = len(actual_calls)
    if actual != expected:
        return GradeResult(False, f"emitted_{actual}_calls_expected_{expected}",
                           expected_calls=expected, actual_calls=actual)

    # Greedy match: each actual call must consume one unmatched GT entry.
    used = [False] * len(ground_truth)
    for call_name, call_args in actual_calls:
        matched = False
        for i, gt_entry in enumerate(ground_truth):
            if used[i]:
                continue
            if _call_matches_gt_entry(call_name, call_args, gt_entry):
                used[i] = True
                matched = True
                break
        if not matched:
            return GradeResult(False, f"call_{call_name}_unmatched_in_gt",
                               expected_calls=expected, actual_calls=actual)
    return GradeResult(True, "all_calls_matched",
                       expected_calls=expected, actual_calls=actual)


def grade_irrelevance(
    actual_calls: list[tuple[str, dict[str, Any]]],
) -> GradeResult:
    """Grade `irrelevance` — model must NOT call any tool. A correct
    response is a refusal / clarifying-question / non-tool reply.
    """
    if len(actual_calls) == 0:
        return GradeResult(True, "correctly_no_tool_call",
                           expected_calls=0, actual_calls=0)
    return GradeResult(False, f"called_tool_when_irrelevant: {actual_calls[0][0]}",
                       expected_calls=0, actual_calls=len(actual_calls))


_GRADERS_BY_CATEGORY = {
    "simple_python": grade_simple,
    "multiple": grade_simple,
    "parallel": grade_parallel,
    "parallel_multiple": grade_parallel,
}


def grade(
    category: str,
    actual_calls: list[tuple[str, dict[str, Any]]],
    ground_truth: list[dict[str, dict[str, list[Any]]]] | None,
) -> GradeResult:
    """Dispatch to the right grader by category.

    `ground_truth` may be None for irrelevance (no gt expected).
    """
    if category == "irrelevance":
        return grade_irrelevance(actual_calls)
    if category not in _GRADERS_BY_CATEGORY:
        return GradeResult(False, f"unsupported_category: {category}")
    if ground_truth is None:
        return GradeResult(False, "missing_ground_truth")
    return _GRADERS_BY_CATEGORY[category](actual_calls, ground_truth)
