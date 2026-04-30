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


# --- strict gates ----------------------------------------------------------
# When any of these triggers on a write-mode task, the run is marked ERROR
# regardless of test/regex outcome. Each gate closes a specific failure mode
# observed in Phase 2: models passing checks via destruction, role-name leaks,
# or placeholder text gaming the test runner.

# Threshold for the destructive-deletion gate: lines_deleted / max(1, lines_added).
# A write-mode PR that deletes 10× more than it adds is almost always a model
# wiping a working file rather than implementing a feature.
_DESTRUCTIVE_DELETION_RATIO = 5.0
# Below this threshold, allow any deletion ratio — small diffs (e.g. removing
# a 12-line dead function) shouldn't trip the gate.
_DESTRUCTIVE_MIN_DELETIONS = 30

# Role names that the orchestrator uses internally; if any leaks into a path
# component of a changed file, the model has confused agent role labels with
# project module names. Seen in Phase 2: src/worker_read.js,
# src/input/worker_analyze/reset.py.
# Multi-word role labels — substring match after tokenizing on _ and -.
_ROLE_FUZZY_NEEDLES = (
    "worker_read", "worker_code", "worker_analyze", "micro_architect",
)
# Single-token role labels — discrete-token match (so "encoder" doesn't
# trip "coder"). "coder" is intentionally excluded.
_ROLE_SINGLE_TOKENS = frozenset({
    "drafter", "verifier", "linter", "architect", "synthesizer", "validator",
})

# Placeholder strings the model has emitted as "implementations". Wider
# patterns than the v1 set — Phase 2 showed the model evading by adding
# extra adjectives ("your real listener code here") or trigger verb variants
# ("attach the listener here").
_PLACEHOLDER_PATTERNS = [
    re.compile(r"<paste\b[^<>]*\bhere\s*>", re.IGNORECASE),
    re.compile(
        r"(?://|#)\s*your\s+(?:real\s+|own\s+|actual\s+)?\w+(?:\s+\w+){0,5}\s+(?:code|here|implementation|logic)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?://|#)\s*(?:add|implement|insert|paste|reset|attach|wire|hook)\s+"
        r"(?:the\s+|a\s+|an\s+)?\w+(?:\s+\w+){0,5}\s+here\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?://|#)\s*(?:fill\s+in|put|place)\s+(?:the\s+|your\s+)?\w+(?:\s+\w+){0,3}\s+here\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?://|#)\s*todo:?\s*(?:implement|add|finish|complete|fill|wire|hook)\s",
               re.IGNORECASE),
    re.compile(r"(?://|#)\s*real\s+\w+(?:\s+\w+){0,3}\s+(?:goes|belongs)\s+here\b",
               re.IGNORECASE),
]


def check_destructive_deletion(additions: int, deletions: int) -> tuple[bool, str]:
    """True if the diff is dominated by deletions. Returns (triggered, detail)."""
    if deletions < _DESTRUCTIVE_MIN_DELETIONS:
        return False, ""
    if additions == 0:
        return True, f"deleted {deletions} lines, added 0"
    ratio = deletions / max(1, additions)
    if ratio >= _DESTRUCTIVE_DELETION_RATIO:
        return True, f"deleted {deletions}, added {additions} (ratio {ratio:.1f}× — destructive)"
    return False, ""


def check_role_name_leak(file_paths: list[str]) -> tuple[bool, str]:
    """True if any path component contains an agent role label (fuzzy)."""
    leaks: list[str] = []
    for path in file_paths:
        for part in path.split("/"):
            stem = part.split(".", 1)[0].lower()
            tokens = re.split(r"[-_]+", stem)
            joined = "_".join(tokens)
            if any(needle in joined for needle in _ROLE_FUZZY_NEEDLES):
                leaks.append(path)
                break
            if any(t in _ROLE_SINGLE_TOKENS for t in tokens):
                leaks.append(path)
                break
    if leaks:
        return True, f"role-name leak in {len(leaks)} path(s): {leaks[:3]}"
    return False, ""


def check_placeholder_text(diff_added_text: str) -> tuple[bool, str]:
    """True if any added line matches a known placeholder pattern."""
    for pat in _PLACEHOLDER_PATTERNS:
        m = pat.search(diff_added_text)
        if m:
            return True, f"placeholder match: {m.group(0)[:80]!r}"
    return False, ""


def _looks_like_test_file(path: str) -> bool:
    """Heuristic: matches common test conventions across JS/TS/Python.

    Catches *test*.{js,ts,jsx,tsx,py}, files in tests/__tests__/spec dirs,
    test_*.py, *_test.{py,go}. Tight enough to skip non-test files like
    "src/utils/test-helpers.js" (which lives under src/ not tests/).
    """
    p = path.lower()
    parts = p.split("/")
    test_dirs = {"tests", "test", "__tests__", "spec", "specs", "__test__"}
    if any(part in test_dirs for part in parts[:-1]):
        return True
    name = parts[-1]
    if name.startswith("test_") and name.endswith((".py", ".go", ".rs")):
        return True
    for suffix in (".test.js", ".test.jsx", ".test.ts", ".test.tsx",
                   ".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx",
                   "_test.py", "_test.go", "_test.rs"):
        if name.endswith(suffix):
            return True
    return False


def check_vacuous_test(
    repo_path: Path,
    base_sha: str,
    command: str,
    changed_files: list[str],
    timeout: float = 600.0,
) -> tuple[bool, str]:
    """Vacuous-test gate: a new test that passes against the unmodified base
    isn't actually testing the implementation.

    Strategy: spin up a git worktree at base_sha, copy the new/modified test
    files from HEAD into it, then run the test command. If rc=0, the test
    file isn't exercising any new behaviour — mark vacuous.

    Returns (vacuous, detail). Returns (False, "...") on infrastructure
    errors (worktree creation failed, etc.) — fail-open so a flaky check
    doesn't downgrade legitimate passes.
    """
    if not base_sha or not command:
        return False, ""
    test_paths = [p for p in changed_files if _looks_like_test_file(p)]
    if not test_paths:
        # No test files in the diff — the implementation must be carrying
        # the existing test suite. Not a vacuous-test concern.
        return False, ""

    import tempfile
    with tempfile.TemporaryDirectory(prefix="luxe-vacuous-") as td:
        wt = Path(td) / "worktree"
        rc, _out = _run(["git", "worktree", "add", "--detach",
                         str(wt), base_sha], cwd=repo_path, timeout=60)
        if rc != 0:
            return False, ""  # fail-open

        try:
            # Copy each new/modified test file from HEAD into the worktree.
            # Existing tests at base SHA stay as-is (the gate cares whether
            # the *new* test passes against unmodified base implementation).
            for rel in test_paths:
                src = repo_path / rel
                if not src.is_file():
                    continue
                dst = wt / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())

            rc, out = _run(["bash", "-lc", command], cwd=wt, timeout=timeout)
            tail = "\n".join((out or "").splitlines()[-15:])[:600]
            if rc == 0:
                return True, (
                    f"new test files {test_paths!r} passed against base SHA "
                    f"({base_sha[:8]}) — test isn't exercising new code; tail:\n{tail}"
                )
            return False, ""
        finally:
            # Best-effort cleanup; tempdir context will reap whatever's left.
            _run(["git", "worktree", "remove", "--force", str(wt)],
                 cwd=repo_path, timeout=60)


def apply_strict_gates(
    *,
    task_type: str,
    file_paths: list[str],
    additions: int,
    deletions: int,
    diff_added_text: str,
) -> list[tuple[str, str]]:
    """Run all three gates; return list of (gate_name, detail) for each
    triggered gate. Empty list = no gates triggered. Read-mode tasks
    (review, summarize) skip these checks since they shouldn't produce
    diffs in the first place.
    """
    if task_type not in _WRITE_TASK_TYPES:
        return []
    triggered: list[tuple[str, str]] = []
    ok, detail = check_destructive_deletion(additions, deletions)
    if ok:
        triggered.append(("destructive_diff", detail))
    ok, detail = check_role_name_leak(file_paths)
    if ok:
        triggered.append(("role_name_leak", detail))
    ok, detail = check_placeholder_text(diff_added_text)
    if ok:
        triggered.append(("placeholder_diff", detail))
    return triggered


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
    # Strict-grading additions (default empty for back-compat with v1 records).
    gates_triggered: list[dict] = field(default_factory=list)
    diff_additions: int = 0
    diff_deletions: int = 0

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
                         changed_files: list[str],
                         base_sha: str = "",
                         min_matches: int = 1,
                         min_added_lines: int = 0) -> tuple[bool, str]:
    """Pattern must appear in the diff's *added* lines, not in pre-existing
    content. Closes the lpe-rope-calc loophole where a model touched a file
    that already contained the pattern.

    Two thresholds beyond a single match:
    - `min_matches` — pattern must hit in at least N distinct added lines.
      Defends against "type one function and call it done" gaming when the
      task scope spans many call sites (e.g., "type EVERY top-level function").
    - `min_added_lines` — diff must add at least N total lines across changed
      files. Defends against rename-only or one-line edits that pass the
      regex without doing substantive work (e.g., the isomer "Quick Start"
      → "Quickstart" rename that stripped the ISOMER_SECRET setup).

    Falls back to whole-file scan when base_sha isn't provided.
    """
    if not pattern:
        return False, "no pattern"
    rx = re.compile(pattern)

    if base_sha and changed_files:
        rc, out = _run(["git", "diff", base_sha, "HEAD", "--",
                        *changed_files], cwd=repo_path)
        if rc == 0 and out:
            added_lines: list[tuple[str, str]] = []  # (file, line)
            current_file = ""
            for line in out.splitlines():
                if line.startswith("+++"):
                    current_file = line[6:] if line.startswith("+++ b/") else line[4:]
                elif line.startswith("+") and not line.startswith("+++"):
                    added_lines.append((current_file, line[1:]))

            if min_added_lines and len(added_lines) < min_added_lines:
                return False, (f"only {len(added_lines)} added lines, "
                               f"need ≥{min_added_lines} (substantive-edit gate)")

            matches: list[str] = []
            for fname, body in added_lines:
                if rx.search(body):
                    matches.append(fname)
                    if len(matches) >= min_matches:
                        break
            if len(matches) >= min_matches:
                if min_matches == 1:
                    return True, f"matched in added line of {matches[0]}"
                return True, (f"matched in {len(matches)} added lines "
                              f"(needed ≥{min_matches}); first: {matches[0]}")
            return False, (f"pattern matched {len(matches)}× in {len(added_lines)} "
                           f"added lines (needed ≥{min_matches}) across "
                           f"{len(changed_files)} changed file(s)")

    for rel in changed_files:
        p = repo_path / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        hits = rx.findall(text)
        if len(hits) >= min_matches:
            return True, f"matched {len(hits)}× in {rel} (whole-file fallback)"
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
        # Vacuous-test gate: a passing test that also passes against the
        # unmodified base SHA isn't exercising the implementation. Closes
        # the hole that let swarm__14b's neon-rain pass slip through.
        if passed:
            vacuous, vac_detail = check_vacuous_test(
                repo_path, base_sha, eo.get("command", ""), changed,
            )
            if vacuous:
                result.expected_outcome_passed = False
                result.expected_outcome_detail = (
                    f"{detail}\n\n[vacuous_test gate] {vac_detail}"
                )
                result.gates_triggered.append({
                    "gate": "vacuous_test",
                    "detail": vac_detail[:500],
                })
                earned_outcome = 0
    elif kind == "regex_present":
        passed, detail = _check_regex_present(
            repo_path, eo.get("pattern", ""), changed, base_sha=base_sha,
            min_matches=int(eo.get("min_matches", 1)),
            min_added_lines=int(eo.get("min_added_lines", 0)),
        )
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
