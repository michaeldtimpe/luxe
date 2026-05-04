"""Mechanical smoke inspection for a SWE-bench `predictions.json`.

Gates a predictions file against four criteria — does NOT replace the
Docker harness's FAIL_TO_PASS / PASS_TO_PASS scoring. Purpose: quickly
detect the prompt-regression we hit in the 2026-05-04 smoke (model
creates reproducer scripts instead of editing source), without paying
the Docker setup cost.

PASS criteria, per instance:
  1. model_patch is non-empty
  2. No new files in the diff (catches reproducer scripts, scratch files)
  3. No diff hunks touch obvious test paths (catches the model
     "fixing the test instead of the bug" failure mode)
  4. At least one non-blank, non-comment +/- line (catches whitespace-
     only or comment-only "fixes")

Usage:
    python -m benchmarks.swebench.smoke_inspect \\
        --predictions acceptance/swebench/<dir>/predictions.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|testing)(?:/|$)"
    r"|(?:^|/)test_[^/]*\.py$"
    r"|(?:^|/)[^/]*_test\.py$"
)


@dataclass
class InstanceVerdict:
    instance_id: str
    passed: bool
    reasons: list[str] = field(default_factory=list)


def _diff_paths(model_patch: str) -> list[str]:
    """Extract `b/<path>` paths from `diff --git a/... b/...` lines."""
    paths: list[str] = []
    for line in model_patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                paths.append(parts[1].strip())
    return paths


def _has_new_file(model_patch: str) -> bool:
    return any(line.startswith("new file mode") for line in model_patch.splitlines())


def _has_substantive_change(model_patch: str) -> bool:
    """At least one +/- line that is not blank, not a comment, not a +++/--- header."""
    for line in model_patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if not (line.startswith("+") or line.startswith("-")):
            continue
        body = line[1:].strip()
        if not body:
            continue
        # Python comment lines — would also catch hash-comments in YAML/conf,
        # which is fine; we want substantive logic edits.
        if body.startswith("#"):
            continue
        return True
    return False


def inspect_instance(instance_id: str, model_patch: str) -> InstanceVerdict:
    reasons: list[str] = []
    if not model_patch.strip():
        reasons.append("empty_patch")
        return InstanceVerdict(instance_id=instance_id, passed=False, reasons=reasons)

    if _has_new_file(model_patch):
        reasons.append("new_file_in_diff")

    paths = _diff_paths(model_patch)
    test_paths = [p for p in paths if _TEST_PATH_RE.search(p)]
    if test_paths:
        reasons.append(f"touches_test_paths={test_paths}")

    if not _has_substantive_change(model_patch):
        reasons.append("no_substantive_change")

    return InstanceVerdict(
        instance_id=instance_id,
        passed=not reasons,
        reasons=reasons,
    )


def inspect_predictions(predictions_path: Path) -> list[InstanceVerdict]:
    rows = json.loads(predictions_path.read_text())
    return [inspect_instance(r["instance_id"], r.get("model_patch", "")) for r in rows]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--predictions", required=True, type=Path)
    args = p.parse_args()

    verdicts = inspect_predictions(args.predictions)
    n_pass = sum(1 for v in verdicts if v.passed)
    n_total = len(verdicts)

    for v in verdicts:
        mark = "PASS" if v.passed else "FAIL"
        reasons = "" if v.passed else f"  reasons={v.reasons}"
        print(f"  {mark}  {v.instance_id}{reasons}")
    print()
    print(f"smoke inspect: {n_pass}/{n_total} pass")
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main())
