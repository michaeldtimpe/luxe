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
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


_WRITE_TASK_TYPES = {"implement", "bugfix", "document", "manage"}


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
    diff_produced: bool = False    # True iff git diff base..HEAD is non-empty
    diff_files: int = 0            # count of changed files (informational)
    skipped: bool = False
    skipped_reason: str = ""
    error: str = ""
    # Per-criterion breakdown so the verdict shows what passed/failed and why.
    criteria_breakdown: list[dict] = field(default_factory=list)

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
    """Run all three grading checks against an already-completed run.

    Scoring (5 pts max, pass = ≥4/5):
      1 pt  — pr.py opened a PR
      3 pts — expected_outcome check passed AGAINST A LUXE-MODIFIED REPO
              (write tasks must produce a non-empty diff vs base_sha; the
              outcome credit is gated on this so passing tests on the
              unchanged base SHA isn't a false positive)
      1 pt  — citation linter found zero unresolved citations
    """
    result = FixtureResult(fixture_id=fixture.id)
    result.pr_url = pr_url
    result.pr_opened = pr_opened
    result.citations_unresolved = citations_unresolved
    result.citations_total = citations_total

    # Compute the diff up front — used both for outcome gating and as a
    # diagnostic surface. Empty list = luxe made no changes.
    changed = _changed_files(repo_path, base_sha) if base_sha else []
    result.diff_files = len(changed)
    result.diff_produced = bool(changed)

    is_write_task = fixture.task_type in _WRITE_TASK_TYPES

    # Criterion 1: PR opened
    if pr_opened:
        result.score += 1
    result.criteria_breakdown.append({
        "criterion": "pr_opened",
        "weight": 1,
        "earned": 1 if pr_opened else 0,
        "detail": (f"PR: {pr_url}" if pr_opened
                   else "no PR opened (no diff or PR cycle blocked)"),
    })

    # Criterion 2: expected_outcome — gated on diff for write tasks
    eo = fixture.expected_outcome
    kind = eo.get("kind", "")
    earned_outcome = 0
    if is_write_task and not result.diff_produced:
        # Refuse to credit: passing tests on unchanged code is a false positive.
        result.expected_outcome_passed = False
        result.expected_outcome_detail = (
            f"luxe produced no diff vs base_sha — "
            f"{kind} outcome NOT credited for write task"
        )
    elif kind == "tests_pass":
        passed, detail = _check_tests_pass(repo_path, eo.get("command", ""))
        result.expected_outcome_passed = passed
        result.expected_outcome_detail = detail
        earned_outcome = 3 if passed else 0
    elif kind == "regex_present":
        passed, detail = _check_regex_present(repo_path, eo.get("pattern", ""), changed)
        result.expected_outcome_passed = passed
        result.expected_outcome_detail = detail
        earned_outcome = 3 if passed else 0
    elif kind == "regex_absent":
        passed, detail = _check_regex_absent(repo_path, eo.get("pattern", ""), changed)
        result.expected_outcome_passed = passed
        result.expected_outcome_detail = detail
        earned_outcome = 3 if passed else 0
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

    result.score += earned_outcome
    result.criteria_breakdown.append({
        "criterion": f"expected_outcome ({kind})",
        "weight": 3,
        "earned": earned_outcome,
        "detail": result.expected_outcome_detail[:200],
        "diff_files": result.diff_files,
    })

    # Criterion 3: zero unresolved citations
    citations_clean = (citations_unresolved == 0)
    if citations_clean:
        result.score += 1
    result.criteria_breakdown.append({
        "criterion": "citations_resolved",
        "weight": 1,
        "earned": 1 if citations_clean else 0,
        "detail": (f"all {citations_total} citations resolved"
                   if citations_clean and citations_total > 0
                   else "no citations" if citations_total == 0 and citations_unresolved == 0
                   else f"{citations_unresolved} unresolved"),
    })

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
