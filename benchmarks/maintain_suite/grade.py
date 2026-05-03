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


# Source-code extensions for the orphan-file gate. Files outside this set
# (markdown, configs, lockfiles, etc.) are legitimate standalone additions
# and aren't subject to the orphan check.
_SOURCE_EXTS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".rs", ".go", ".java", ".kt", ".swift",
    ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
    ".rb", ".php", ".cs", ".scala",
})


def _is_source_path(path: str) -> bool:
    """A code source file (not a test, doc, or config)."""
    if _looks_like_test_file(path):
        return False
    return Path(path).suffix.lower() in _SOURCE_EXTS


def _added_files_in_diff(repo_path: Path, base_sha: str) -> list[str]:
    """Return paths of files newly added (status A) in base_sha..HEAD."""
    rc, out = _run(["git", "diff", "--name-status", base_sha, "HEAD"],
                   cwd=repo_path)
    if rc != 0:
        return []
    added: list[str] = []
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0] == "A":
            added.append(parts[1].strip())
    return added


def check_orphan_file(
    repo_path: Path,
    base_sha: str,
    task_type: str,
) -> tuple[bool, str]:
    """Detect new source files that nothing imports.

    Catches the "orphan-file" exploit where a model adds a from-scratch
    source file (often in a parallel language to the project's actual
    stack) that satisfies the regex/test grader, but nothing imports it
    and the existing tests still pass because the existing implementation
    is unchanged. Seen in the granite-3b bake-off (2026-04-30): model
    added `src/input/HtmlInputHandler.ts` next to the existing `.js`
    file in a JS-only project; npm test passed because nothing references
    the new TypeScript file.

    Two-prong detection. A NEW source file is orphan iff EITHER:
      1. A sibling file with the same stem in the same directory already
         existed (e.g. `Foo.ts` next to `Foo.js`). Strong duplicate signal.
      2. The file's stem isn't referenced anywhere else in the post-edit
         repo — no import, require, or path-string mentions it.

    Only applies to implement/bugfix tasks. document/manage tasks
    legitimately add standalone files (CONFIG.md, ARCHITECTURE.md,
    SECURITY-AUDIT.md) that aren't expected to be imported.

    Returns (triggered, detail). Fail-open on infrastructure errors.
    """
    if task_type not in ("implement", "bugfix"):
        return False, ""
    if not base_sha:
        return False, ""

    new_files = [p for p in _added_files_in_diff(repo_path, base_sha)
                 if _is_source_path(p)]
    if not new_files:
        return False, ""

    orphans: list[str] = []
    for new_path in new_files:
        new_stem = Path(new_path).stem
        if not new_stem or new_stem.startswith("_"):
            # __init__.py, _internal.py, etc. — legitimate bare-stem cases.
            continue

        # Prong 1: same-directory sibling with same stem (different ext).
        new_dir = (repo_path / new_path).parent
        if new_dir.is_dir():
            for sibling in new_dir.iterdir():
                if not sibling.is_file():
                    continue
                try:
                    rel = str(sibling.relative_to(repo_path))
                except ValueError:
                    continue
                if rel == new_path:
                    continue
                if sibling.stem == new_stem and _is_source_path(rel):
                    orphans.append(
                        f"{new_path} duplicates existing {rel} "
                        "(same stem, same dir — orphan duplicate)"
                    )
                    break
            else:
                # No same-stem sibling — fall through to Prong 2.
                pass
            if orphans and orphans[-1].startswith(new_path):
                continue

        # Prong 2: any reference to the stem elsewhere in source files?
        # `git grep -w` does a whole-word match on the stem across tracked
        # source files. If the only match is the new file itself, it's
        # orphan; if zero matches, also orphan.
        rc, out = _run(
            ["git", "grep", "-l", "-w", "--", new_stem,
             "*.py", "*.js", "*.jsx", "*.ts", "*.tsx", "*.mjs", "*.cjs",
             "*.rs", "*.go", "*.java", "*.kt", "*.swift",
             "*.cpp", "*.cc", "*.cxx", "*.c", "*.h", "*.hpp",
             "*.rb", "*.php", "*.cs", "*.scala"],
            cwd=repo_path,
        )
        matches: set[str] = set()
        if rc == 0 and out.strip():
            matches = {l.strip() for l in out.splitlines() if l.strip()}
        matches.discard(new_path)
        if not matches:
            orphans.append(
                f"{new_path} (stem `{new_stem}` not referenced anywhere "
                "else in the post-edit repo — orphan)"
            )

    if orphans:
        return True, "; ".join(orphans[:3])
    return False, ""


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
    # SpecDD Lever 1 (v1.4-prep) parallel observation. Populated when a
    # fixture has `requirements:` authored. Does NOT gate score (legacy
    # `expected_outcome` still drives the binary PASS/FAIL); spec_validation
    # is observability for the migration. Each entry: {id, must, satisfied,
    # detail}. spec_all_satisfied is None when no requirements were authored.
    spec_validation: list[dict] = field(default_factory=list)
    spec_all_satisfied: bool | None = None

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
    # SpecDD Lever 1 (v1.4-prep): list of raw requirement dicts as authored
    # in fixtures.yaml. Empty list = legacy fixture (uses expected_outcome
    # only). Validated lazily via to_spec(); not eagerly because fixtures
    # without requirements should still load on systems where src/luxe/spec.py
    # isn't yet importable for any reason.
    requirements: list[dict] = field(default_factory=list)

    def to_spec(self):
        """Return a Spec if requirements are authored, else None.

        Imported lazily so legacy fixture loads don't depend on luxe.spec.
        """
        if not self.requirements:
            return None
        from luxe.spec import spec_from_yaml_dict
        return spec_from_yaml_dict({
            "goal": self.goal,
            "requirements": self.requirements,
        })

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
            requirements=list(d.get("requirements", [])),
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

            # Count matches across ALL added lines (no early-break) so the
            # error message can report the true match count when min_matches
            # is the deeper failure.
            matches: list[str] = []
            for fname, body in added_lines:
                if rx.search(body):
                    matches.append(fname)

            # When min_matches > 1, evaluate it BEFORE min_added_lines so the
            # more informative message wins. The lpe-rope-calc-document-typing
            # v1 baseline failure (2026-04-30) reported a misleading
            # "min_added_lines" message when the deeper issue was the model
            # only typing 3 of N functions. OR-failure semantics preserved:
            # either floor failing makes the outcome fail.
            if min_matches > 1 and len(matches) < min_matches:
                return False, (f"pattern matched {len(matches)}× in "
                               f"{len(added_lines)} added lines "
                               f"(needed ≥{min_matches}) across "
                               f"{len(changed_files)} changed file(s)")

            if min_added_lines and len(added_lines) < min_added_lines:
                return False, (f"only {len(added_lines)} added lines, "
                               f"need ≥{min_added_lines} (substantive-edit gate)")

            if len(matches) >= min_matches:
                if min_matches == 1:
                    return True, f"matched in added line of {matches[0]}"
                return True, (f"matched in {len(matches)} added lines "
                              f"(needed ≥{min_matches}); first: {matches[0]}")
            # min_matches == 1 case where 0 matches found.
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


_SHORTSTAT_INS_RX = re.compile(r"(\d+) insertions?\(\+\)")
_SHORTSTAT_DEL_RX = re.compile(r"(\d+) deletions?\(-\)")


def _diff_shortstat(repo_path: Path, base_sha: str) -> tuple[int, int]:
    """Return (additions, deletions) from `git diff base_sha HEAD --shortstat`.

    Empty diff or git failure → (0, 0). Tolerates --shortstat's omit-when-zero
    behavior (a pure-add diff has no `deletions(-)` clause and vice versa).
    """
    if not base_sha:
        return 0, 0
    rc, out = _run(["git", "diff", base_sha, "HEAD", "--shortstat"],
                   cwd=repo_path)
    if rc != 0 or not out.strip():
        return 0, 0
    ins = _SHORTSTAT_INS_RX.search(out)
    dels = _SHORTSTAT_DEL_RX.search(out)
    return (int(ins.group(1)) if ins else 0,
            int(dels.group(1)) if dels else 0)


def _diff_added_text(repo_path: Path, base_sha: str,
                     changed_files: list[str]) -> str:
    """Concatenate the `+` lines of `git diff base_sha HEAD -- *changed_files`.

    Used by apply_strict_gates to detect placeholder text in only newly-added
    content (so pre-existing placeholders elsewhere don't trigger the gate).
    Strips the leading `+` and skips `+++` file-header markers.
    """
    if not base_sha or not changed_files:
        return ""
    rc, out = _run(["git", "diff", base_sha, "HEAD", "--", *changed_files],
                   cwd=repo_path)
    if rc != 0 or not out:
        return ""
    added: list[str] = []
    for line in out.splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
    return "\n".join(added)


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

    # Capture diff size for strict-gate inputs and for the diagnostic surface
    # (Phase 0 Bug 1: these were declared on FixtureResult but never populated,
    # which kept downstream gate logic from ever seeing the destructive-diff
    # signal).
    if base_sha and result.diff_produced:
        result.diff_additions, result.diff_deletions = _diff_shortstat(
            repo_path, base_sha)

    is_write_task = fixture.task_type in _WRITE_TASK_TYPES

    # Strict gates — fire on the diff itself, before outcome credit. A
    # write task that triggers any gate (destructive_diff / role_name_leak /
    # placeholder_diff) cannot pass; the gate detail becomes the failure
    # reason. Phase 0 Bug 2: apply_strict_gates was defined but never
    # invoked from grade_fixture, so destructive diffs and placeholder
    # stubs were silently credited.
    gate_block: tuple[str, str] | None = None
    if is_write_task and result.diff_produced:
        added_text = _diff_added_text(repo_path, base_sha, changed)
        triggered = apply_strict_gates(
            task_type=fixture.task_type,
            file_paths=changed,
            additions=result.diff_additions,
            deletions=result.diff_deletions,
            diff_added_text=added_text,
        )
        for name, detail in triggered:
            result.gates_triggered.append({"name": name, "detail": detail})
        if triggered and gate_block is None:
            gate_block = triggered[0]

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
    if gate_block is not None:
        # A pre-outcome strict gate fired (destructive diff, role-name leak,
        # placeholder text). The diff is disqualified regardless of whether
        # the surface-level outcome would have credited; the gate detail
        # becomes the failure reason.
        gate_name, gate_detail = gate_block
        result.expected_outcome_passed = False
        result.expected_outcome_detail = f"[{gate_name}] {gate_detail}"
    elif is_write_task and not result.diff_produced:
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
        # unmodified base SHA isn't exercising the implementation.
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
            else:
                # Orphan-file gate: a NEW source file added by an
                # implement/bugfix task that nothing imports. Tests pass
                # against the unchanged existing implementation; the new
                # file is a phantom satisfying the grader.
                orphan, orph_detail = check_orphan_file(
                    repo_path, base_sha, fixture.task_type,
                )
                if orphan:
                    result.expected_outcome_passed = False
                    result.expected_outcome_detail = (
                        f"{detail}\n\n[orphan_file gate] {orph_detail}"
                    )
                    result.gates_triggered.append({
                        "gate": "orphan_file",
                        "detail": orph_detail[:500],
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
        # Orphan-file gate also applies to regex_present passes on
        # implement/bugfix tasks — the model could match a regex by
        # adding an unwired source file just as readily.
        if passed:
            orphan, orph_detail = check_orphan_file(
                repo_path, base_sha, fixture.task_type,
            )
            if orphan:
                result.expected_outcome_passed = False
                result.expected_outcome_detail = (
                    f"{detail}\n\n[orphan_file gate] {orph_detail}"
                )
                result.gates_triggered.append({
                    "gate": "orphan_file",
                    "detail": orph_detail[:500],
                })
                earned_outcome = 0
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

    # SpecDD Lever 1 (v1.4-prep) parallel observation: when the fixture has
    # `requirements:` authored, run the spec validator alongside the legacy
    # grader and record per-requirement results. Does NOT change result.score
    # — `expected_outcome` is still the binary gate. This lets us compare
    # spec output to legacy output as fixtures migrate.
    spec = fixture.to_spec()
    if spec is not None and base_sha:
        from luxe.spec_validator import validate as _validate_spec
        validation = _validate_spec(spec, repo_path, base_sha)
        result.spec_all_satisfied = validation.all_satisfied
        result.spec_validation = [
            {
                "id": r.requirement.id,
                "must": r.requirement.must,
                "satisfied": r.satisfied,
                "detail": r.detail,
            }
            for r in validation.results
        ]

    return result


def summarize(
    results: list[FixtureResult],
    *,
    per_variant: dict[str, list[FixtureResult]] | None = None,
) -> dict[str, Any]:
    """Aggregate per-fixture results into a release-gate summary.

    `per_variant` is the multi-cell breakdown when the bench ran a
    --variants matrix. The v1 release gate is per-cell, NOT aggregate:
    a 6-cell × 10-fixture matrix with 33 total passes is NOT a release
    pass if no individual cell hits ≥8/10. We compute the gate as
    True iff ANY variant has ≥8 fresh passes over ≥10 fixtures.

    Single-variant (or no-variant) runs use the flat results list.
    """
    total = len(results)
    skipped = sum(1 for r in results if r.skipped)
    errored = sum(1 for r in results if r.error)
    passed = sum(1 for r in results if r.passed)
    total_pts = sum(r.score for r in results)
    max_pts = sum(r.max_score for r in results)

    if per_variant:
        # Multi-variant: gate is True iff some variant clears 8/10.
        per_variant_gate: dict[str, bool] = {}
        any_cleared = False
        for vid, vresults in per_variant.items():
            v_passed = sum(1 for r in vresults if r.passed)
            v_total = len(vresults)
            cleared = v_passed >= 8 and v_total >= 10
            per_variant_gate[vid] = cleared
            if cleared:
                any_cleared = True
        gate = any_cleared
    else:
        per_variant_gate = {}
        gate = passed >= 8 and total >= 10

    return {
        "fixtures": total,
        "passed": passed,
        "failed": total - passed - skipped - errored,
        "skipped": skipped,
        "errored": errored,
        "score": total_pts,
        "max_score": max_pts,
        "v1_release_gate": gate,
        "v1_release_gate_per_variant": per_variant_gate,
    }


def fixture_pass_threshold(score: int, max_score: int = 5) -> bool:
    """Pass = score ≥ 4 of 5."""
    return score >= 4
