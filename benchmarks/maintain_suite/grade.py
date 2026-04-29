"""Automated grader for the v1.0 acceptance suite.

Per plan §10: each fixture earns up to 5 points:
  - 1 pt — `luxe maintain` opened a PR (status_done; not failed_no_mutations)
  - 3 pts — expected_outcome check passed
  - 1 pt — citation linter found zero unresolved citations

A fixture passes when it earns ≥4/5 points. v1.0 ships when ≥8/10 pass.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FixtureResult:
    """One fixture's grading record. Persisted as JSON next to the run dir."""
    fixture_id: str
    score: int = 0
    max_score: int = 5
    pr_opened: bool = False
    pr_url: str = ""
    expected_outcome_passed: bool | None = None  # None = skipped (manual)
    expected_outcome_detail: str = ""
    citations_unresolved: int = 0
    citations_total: int = 0
    skipped: bool = False
    skipped_reason: str = ""
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.score >= 4 and not self.skipped and not self.error

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


@dataclass
class Fixture:
    id: str
    goal: str
    task_type: str
    expected_outcome: dict[str, Any]
    repo_url: str = ""
    repo_path: str = ""
    base_sha: str = ""
    required_env: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Fixture":
        return cls(
            id=str(d.get("id", "")),
            goal=str(d.get("goal", "")),
            task_type=str(d.get("task_type", "review")),
            expected_outcome=dict(d.get("expected_outcome", {})),
            repo_url=str(d.get("repo_url", "")),
            repo_path=str(d.get("repo_path", "")),
            base_sha=str(d.get("base_sha", "")),
            required_env=list(d.get("required_env", [])),
            notes=str(d.get("notes", "")),
        )


def _run(cmd: list[str], cwd: str | Path | None = None,
         timeout: float | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True,
                              check=False, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "[timed out]"
    except FileNotFoundError as e:
        return 127, str(e)


# --- expected_outcome checkers ---------------------------------------------

def _check_tests_pass(repo_path: Path, command: str,
                      timeout: float = 600.0) -> tuple[bool, str]:
    if not command:
        return False, "no command"
    rc, out = _run(["bash", "-lc", command], cwd=repo_path, timeout=timeout)
    detail = out.splitlines()[-30:] if out else []
    return rc == 0, (f"rc={rc}; tail:\n" + "\n".join(detail))[:1500]


def _check_regex_present(repo_path: Path, pattern: str,
                         changed_files: list[str]) -> tuple[bool, str]:
    if not pattern:
        return False, "no pattern"
    rx = re.compile(pattern)
    for rel in changed_files:
        p = repo_path / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        if rx.search(text):
            return True, f"matched in {rel}"
    return False, f"pattern not found in {len(changed_files)} changed files"


def _check_regex_absent(repo_path: Path, pattern: str,
                        changed_files: list[str]) -> tuple[bool, str]:
    if not pattern:
        return False, "no pattern"
    rx = re.compile(pattern)
    for rel in changed_files:
        p = repo_path / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        if rx.search(text):
            return False, f"pattern matched in {rel} (should be absent)"
    return True, f"pattern absent in {len(changed_files)} changed files"


def _changed_files(repo_path: Path, base_sha: str) -> list[str]:
    rc, out = _run(["git", "diff", "--name-only", base_sha, "HEAD"], cwd=repo_path)
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


# --- main entry ------------------------------------------------------------

def grade_fixture(
    fixture: Fixture,
    repo_path: Path,
    *,
    pr_url: str,
    pr_opened: bool,
    citations_unresolved: int,
    citations_total: int,
    base_sha: str,
) -> FixtureResult:
    """Run all three grading checks against an already-completed run."""
    result = FixtureResult(fixture_id=fixture.id)
    result.pr_url = pr_url
    result.pr_opened = pr_opened
    result.citations_unresolved = citations_unresolved
    result.citations_total = citations_total

    if pr_opened:
        result.score += 1

    eo = fixture.expected_outcome
    kind = eo.get("kind", "")
    if kind == "tests_pass":
        passed, detail = _check_tests_pass(repo_path, eo.get("command", ""))
        result.expected_outcome_passed = passed
        result.expected_outcome_detail = detail
        if passed:
            result.score += 3
    elif kind == "regex_present":
        changed = _changed_files(repo_path, base_sha)
        passed, detail = _check_regex_present(repo_path, eo.get("pattern", ""), changed)
        result.expected_outcome_passed = passed
        result.expected_outcome_detail = detail
        if passed:
            result.score += 3
    elif kind == "regex_absent":
        changed = _changed_files(repo_path, base_sha)
        passed, detail = _check_regex_absent(repo_path, eo.get("pattern", ""), changed)
        result.expected_outcome_passed = passed
        result.expected_outcome_detail = detail
        if passed:
            result.score += 3
    elif kind == "manual_review":
        result.expected_outcome_passed = None
        result.expected_outcome_detail = (
            f"manual_review: {eo.get('criteria', '')} — "
            "grader awards 0; review by hand and edit the result file"
        )
        # Manual review awards 0 points until someone hand-edits the result.
    else:
        result.expected_outcome_passed = False
        result.expected_outcome_detail = f"unknown outcome kind: {kind}"

    if citations_unresolved == 0:
        result.score += 1

    return result


def summarize(results: list[FixtureResult]) -> dict[str, Any]:
    total = len(results)
    skipped = sum(1 for r in results if r.skipped)
    errored = sum(1 for r in results if r.error)
    passed = sum(1 for r in results if r.passed)
    total_pts = sum(r.score for r in results)
    max_pts = sum(r.max_score for r in results)
    return {
        "fixtures": total,
        "passed": passed,
        "failed": total - passed - skipped - errored,
        "skipped": skipped,
        "errored": errored,
        "score": total_pts,
        "max_score": max_pts,
        "v1_release_gate": passed >= 8 and total >= 10,
    }


def fixture_pass_threshold(score: int, max_score: int = 5) -> bool:
    """Pass = score ≥ 4 of 5."""
    return score >= 4
