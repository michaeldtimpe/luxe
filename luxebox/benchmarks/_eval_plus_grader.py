"""Self-contained grader for HumanEval+ and MBPP+.

Executes the candidate against each base_input / plus_input, compares the
output to what the canonical solution produces on the same input. Runs each
invocation in a subprocess with a timeout so runaway / crashing candidates
can't hang the sweep.

Deliberately avoids evalplus's `check_correctness` which in 0.3.1 requires
pre-computed `expected_output` dicts we'd have to feed through its
groundtruth pipeline.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_RUNNER_TEMPLATE = '''\
import json, sys, math
{solution}

_inputs = json.loads({inputs_json!r})
_results = []
for args in _inputs:
    try:
        out = {entry_point}(*args)
    except BaseException as e:
        _results.append({{"error": f"{{type(e).__name__}}: {{e}}"}})
        continue
    try:
        json.dumps(out)
        _results.append({{"ok": out}})
    except TypeError:
        _results.append({{"ok": repr(out)}})
sys.stdout.write(json.dumps(_results))
'''


def grade_eval_plus(problem: dict[str, Any], completion_code: str, timeout_s: float = 8.0) -> tuple[bool, bool, str | None]:
    """Returns (base_passed, plus_passed, error_or_None)."""
    entry_point = problem["entry_point"]
    # HumanEval+ "prompt" is `<imports>\n\ndef <entry>(...): """doc"""\n`.
    # If the model emitted a full function, we'd duplicate the signature by
    # prepending the whole prompt — but we still need the imports. Splice
    # on just the preamble (everything before `def <entry>`).
    if _looks_like_full_function(completion_code, entry_point):
        preamble = _prompt_preamble_before_def(problem["prompt"], entry_point)
        candidate = preamble + completion_code
    else:
        candidate = problem["prompt"] + completion_code

    canonical = problem["prompt"] + problem["canonical_solution"]

    base_inputs = problem.get("base_input") or []
    plus_inputs = problem.get("plus_input") or []

    try:
        cand_base = _exec(candidate, entry_point, base_inputs, timeout_s)
        cand_plus = _exec(candidate, entry_point, plus_inputs, timeout_s) if plus_inputs else []
        gold_base = _exec(canonical, entry_point, base_inputs, timeout_s)
        gold_plus = _exec(canonical, entry_point, plus_inputs, timeout_s) if plus_inputs else []
    except _GraderError as e:
        return False, False, str(e)

    atol = problem.get("atol", 0) or 0
    base_ok = _compare(cand_base, gold_base, atol)
    plus_ok = _compare(cand_plus, gold_plus, atol) if plus_inputs else True
    return base_ok, plus_ok, None


class _GraderError(RuntimeError):
    pass


def _exec(solution: str, entry_point: str, inputs: list, timeout_s: float) -> list[dict[str, Any]]:
    script = _RUNNER_TEMPLATE.format(
        solution=solution,
        inputs_json=json.dumps(inputs),
        entry_point=entry_point,
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "run.py"
        path.write_text(script)
        try:
            res = subprocess.run(  # noqa: S603
                [sys.executable, str(path)],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise _GraderError(f"timeout after {timeout_s}s") from e
        if res.returncode != 0:
            raise _GraderError(f"runner exited {res.returncode}: {res.stderr[:500]}")
        try:
            return json.loads(res.stdout or "[]")
        except json.JSONDecodeError as e:
            raise _GraderError(f"runner stdout not JSON: {e}; stderr: {res.stderr[:500]}") from e


def _compare(cand: list, gold: list, atol: float) -> bool:
    if len(cand) != len(gold):
        return False
    for c, g in zip(cand, gold):
        if "error" in g:
            # Canonical errored — skip this input (shouldn't happen in practice).
            continue
        if "error" in c:
            return False
        if not _equal(c["ok"], g["ok"], atol):
            return False
    return True


def _equal(a: Any, b: Any, atol: float) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(float(a), float(b), abs_tol=atol or 1e-9)
        except (TypeError, ValueError):
            return False
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y, atol) for x, y in zip(a, b))
    return a == b


def _looks_like_full_function(code: str, entry_point: str) -> bool:
    """Crude: does the code already contain `def <entry_point>(`?"""
    return f"def {entry_point}(" in code


def _prompt_preamble_before_def(prompt: str, entry_point: str) -> str:
    """Return everything in the prompt that precedes `def <entry_point>(`.

    This is the import block + any module-level helpers that the task
    expects to be in scope. When the model emits a full function, we splice
    this in front of the model's code so names like `List` (from
    `typing.List`) resolve.
    """
    marker = f"def {entry_point}("
    idx = prompt.find(marker)
    if idx < 0:
        return ""
    line_start = prompt.rfind("\n", 0, idx)
    if line_start < 0:
        return ""
    return prompt[: line_start + 1]
