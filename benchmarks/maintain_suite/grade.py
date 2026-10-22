"""Automated grader for the v1.0 acceptance suite.

Per plan §10: each fixture earns up to 5 points:
  - 1 pt — `luxe maintain` opened a PR (status_done; not failed_no_mutations)
  - 3 pts — expected_outcome check passed
  - 1 pt — citation linter found zero unresolved citations

A fixture passes when it earns ≥4/5 points. v1.0 ships when ≥8/10 pass.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from benchmarks.maintain_suite.gates import (
    _MARKDOWN_EXTS,
    _WRITE_TASK_TYPES,
    DocHeadings,
    _unfenced_headings,
    apply_strict_gates,
    check_orphan_file,
    check_vacuous_test,
)
from benchmarks.maintain_suite.proc import (
    GitRunError,
    run as _run,
    run_git as _run_git,
)


def fixture_passed(score: int, *, skipped: bool = False,
                   error: str = "") -> bool:
    """THE pass rule: score >= 4 of 5, and neither skipped nor errored.
    FixtureResult.passed and scripts/regrade_local.py (stored result.json)
    both call this, so the bench and the regrade cannot disagree."""
    return bool(score >= 4 and not skipped and not error)


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
        return fixture_passed(self.score, skipped=self.skipped,
                              error=self.error)

    def to_dict(self) -> dict:
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
    # SpecDD Lever 2 (v1.6-style maintain_suite extension, 2026-05-10):
    # optional list of glob patterns the model may not *create* (it may
    # still freely edit any existing file of the same name). Empty list =
    # no synthesized contract injected. Surfaced by the m5max_moe bake-off
    # post-loop-boundary-fix on neon-rain-implement-reset-shortcut, where
    # GLM produced a vacuous orphan test file when given full agentic
    # loop runway. Mirrors the swebench adapter's Forbids creating
    # pattern; see benchmarks/maintain_suite/run.py:_resolve_repo for
    # the on-disk injection mechanism.
    forbids_create: list[str] = field(default_factory=list)

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
            forbids_create=list(d.get("forbids_create", [])),
            notes=str(d.get("notes", "")),
        )


# --- expected_outcome checkers ---------------------------------------------

def _check_tests_pass(repo_path: Path, command: str,
                      timeout: float = 600.0) -> tuple[bool, str]:
    if not command:
        return False, "no command"
    rc, out = _run(["bash", "-lc", command], cwd=repo_path, timeout=timeout)
    detail = out.splitlines()[-30:] if out else []
    return rc == 0, (f"rc={rc}; tail:\n" + "\n".join(detail))[:1500]


# The substantive-edit gate's unit for prose. An unwrapped markdown paragraph
# is ONE physical line however long it is, so a raw line count measured
# wrapping style: the 2026-10 harness bake-off printed 1/5 on 20 hand-verified
# REAL doc runs (48-149 words of unwrapped prose against 4- and 8-line floors)
# while the same words wrapped at ~80 columns passed. Counting each markdown
# line as ceil(len/80) units can only RAISE a count, so no earlier pass flips,
# and a heading rename or a one-liner is still under 80 columns = 1 unit.
# Code lines always count 1: a long code line is not more code.
_PROSE_LINE_COLUMNS = 80


def _added_line_units(path: str, line: str) -> int:
    """How many lines one added line counts as toward `min_added_lines`."""
    if Path(path).suffix.lower() not in _MARKDOWN_EXTS:
        return 1
    return max(1, -(-len(line) // _PROSE_LINE_COLUMNS))


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
      → "Quickstart" rename that stripped the ISOMER_SECRET setup). Markdown
      lines count in 80-column units (`_added_line_units`), so the floor
      measures how much prose was added, not how it was wrapped.

    Falls back to whole-file scan when base_sha isn't provided.
    """
    if not pattern:
        return False, "no pattern"
    rx = re.compile(pattern)

    if base_sha and changed_files:
        _rc, out = _run_git(["git", "diff", "-M", base_sha, "HEAD", "--",
                             *_diff_pathspec(repo_path, base_sha, changed_files)],
                            cwd=repo_path)
        if out:
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

            if min_added_lines:
                units = sum(_added_line_units(f, b) for f, b in added_lines)
                if units < min_added_lines:
                    shown = (f"{len(added_lines)} added lines"
                             if units == len(added_lines) else
                             f"{len(added_lines)} added lines "
                             f"({units} at 80 columns)")
                    return False, (f"only {shown}, need ≥{min_added_lines} "
                                   f"(substantive-edit gate)")

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


_ABSENT_SCAN_MAX_BYTES = 2_000_000


def _base_files_matching(repo_path: Path, base_sha: str,
                         rx: "re.Pattern[str]") -> list[str]:
    """Tracked files whose base_sha content matches `rx` — the files a
    regex_absent task is actually about. One `ls-tree` + one streamed
    `cat-file --batch`, so Python's regex (not git grep's dialect) decides."""
    _rc, out = _run_git(["git", "ls-tree", "-r", "-z", base_sha], cwd=repo_path)
    entries: list[tuple[str, str]] = []  # (blob sha, path)
    for rec in out.split("\0"):
        if not rec:
            continue
        meta, _tab, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            entries.append((parts[2], path))
    if not entries:
        return []
    try:
        proc = subprocess.run(
            ["git", "cat-file", "--batch"], cwd=str(repo_path),
            input="".join(f"{sha}\n" for sha, _ in entries).encode(),
            capture_output=True, check=False, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise GitRunError(f"git cat-file --batch (cwd={repo_path}) failed: {e}")
    if proc.returncode != 0:
        raise GitRunError(f"git cat-file --batch (cwd={repo_path}) exited "
                          f"{proc.returncode}: {proc.stderr.decode(errors='replace')[-300:]}")
    data = proc.stdout
    pos = 0
    hits: list[str] = []
    for _sha, path in entries:
        nl = data.index(b"\n", pos)
        header = data[pos:nl].split()
        size = int(header[2])
        body = data[nl + 1: nl + 1 + size]
        pos = nl + 1 + size + 1
        if size > _ABSENT_SCAN_MAX_BYTES or b"\0" in body[:8000]:
            continue
        if rx.search(body.decode("utf-8", errors="replace")):
            hits.append(path)
    return hits


def _check_regex_absent(repo_path: Path, pattern: str,
                        changed_files: list[str],
                        base_sha: str = "") -> tuple[bool, str, list[str]]:
    """Pattern must be absent from the files the task is about.

    Returns (passed, detail, deleted_targets). The scan set is the changed
    files PLUS every file that matched at base_sha. Scanning only the
    changed files let a model pass by editing an unrelated file while the
    target still matched, and skipping files missing from the final tree let
    it pass by deleting the target — `deleted_targets` names those so the
    caller can gate them.
    """
    if not pattern:
        return False, "no pattern", []
    rx = re.compile(pattern)
    targets = _base_files_matching(repo_path, base_sha, rx) if base_sha else []
    scan = list(dict.fromkeys([*changed_files, *targets]))
    deleted = [rel for rel in targets if not (repo_path / rel).is_file()]
    for rel in scan:
        p = repo_path / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        if rx.search(text):
            return False, f"pattern matched in {rel} (should be absent)", deleted
    return True, (f"pattern absent in {len(scan)} file(s) "
                  f"({len(targets)} matched at base, {len(changed_files)} changed)"), deleted


def _rename_sources(repo_path: Path, base_sha: str) -> list[str]:
    """Old paths of files renamed in base_sha..HEAD (`R<score>` rows)."""
    _rc, out = _run_git(["git", "diff", "--name-status", "-M", base_sha, "HEAD"],
                        cwd=repo_path)
    olds: list[str] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("R"):
            olds.append(parts[1])
    return olds


def _diff_pathspec(repo_path: Path, base_sha: str,
                   changed_files: list[str]) -> list[str]:
    """`changed_files` plus the OLD side of every rename.

    `_changed_files` lists a renamed file under its new name only, and a
    pathspec of just the new name hides the old one from git's rename
    detection — so a pure `git mv` diffed as a brand-new file whose every
    line was "added", and regex_present credited moved content as new work.
    With both sides in the pathspec and `-M`, a pure rename contributes no
    added lines.
    """
    olds = [p for p in _rename_sources(repo_path, base_sha) if p not in changed_files]
    return [*changed_files, *olds]


def _changed_files(repo_path: Path, base_sha: str) -> list[str]:
    _rc, out = _run_git(["git", "diff", "--name-only", base_sha, "HEAD"],
                        cwd=repo_path)
    return [line.strip() for line in out.splitlines() if line.strip()]


_SHORTSTAT_INS_RX = re.compile(r"(\d+) insertions?\(\+\)")
_SHORTSTAT_DEL_RX = re.compile(r"(\d+) deletions?\(-\)")


def _diff_shortstat(repo_path: Path, base_sha: str) -> tuple[int, int]:
    """Return (additions, deletions) from `git diff base_sha HEAD --shortstat`.

    Empty diff → (0, 0); a FAILED git raises GitRunError instead of reporting
    the zero-diff shape. Tolerates --shortstat's omit-when-zero behavior (a
    pure-add diff has no `deletions(-)` clause and vice versa).
    """
    if not base_sha:
        return 0, 0
    _rc, out = _run_git(["git", "diff", base_sha, "HEAD", "--shortstat"],
                        cwd=repo_path)
    if not out.strip():
        return 0, 0
    ins = _SHORTSTAT_INS_RX.search(out)
    dels = _SHORTSTAT_DEL_RX.search(out)
    return (int(ins.group(1)) if ins else 0,
            int(dels.group(1)) if dels else 0)


def _diff_hunks(repo_path: Path, base_sha: str,
                changed_files: list[str]) -> dict[str, tuple[list[str], list[str]]]:
    """Parse `git diff base_sha HEAD -- *changed_files` into per-file
    (added_lines, deleted_lines), leading +/- stripped.

    The shortstat the destructive gate used to run on says only how MUCH
    changed; the gates added 2026-08-19 need to know WHAT changed (whether the
    removed vocabulary reappears, whether a heading's body went with it,
    whether the added lines carry read_file's numbering), and they need it per
    file so one file's additions can't mask another's destruction.

    `---`/`+++` are only treated as file headers inside a `diff --git` header
    block, so a deleted markdown rule (`--- foo` arrives as `---- foo`, but
    `-- foo` arrives as `--- foo`) is never mistaken for one.
    """
    if not base_sha or not changed_files:
        return {}
    _rc, out = _run_git(["git", "diff", "-M", base_sha, "HEAD", "--",
                         *_diff_pathspec(repo_path, base_sha, changed_files)],
                        cwd=repo_path)
    return _parse_diff_hunks(out)


def _parse_diff_hunks(out: str) -> dict[str, tuple[list[str], list[str]]]:
    """The unified-diff parser behind `_diff_hunks`, split out so it can be
    exercised on diff text from any pair of revisions."""
    hunks: dict[str, tuple[list[str], list[str]]] = {}
    if not out:
        return hunks

    def _slot(path: str) -> tuple[list[str], list[str]]:
        return hunks.setdefault(path, ([], []))

    current = ""
    old_path = ""
    in_header = False
    for line in out.splitlines():
        if line.startswith("diff --git "):
            in_header = True
            current = ""
            old_path = ""
            continue
        if in_header:
            if line.startswith("--- "):
                old_path = line[4:]
                continue
            if line.startswith("+++ "):
                new_path = line[4:]
                if new_path == "/dev/null":
                    path = old_path[2:] if old_path.startswith("a/") else old_path
                else:
                    path = new_path[2:] if new_path.startswith("b/") else new_path
                current = path
                _slot(current)
                in_header = False
                continue
            if line.startswith("@@"):
                in_header = False
            else:
                continue
        if not current:
            continue
        if line.startswith("+"):
            _slot(current)[0].append(line[1:])
        elif line.startswith("-"):
            _slot(current)[1].append(line[1:])
    return hunks


def _doc_headings(repo_path: Path, base_sha: str,
                  hunks: dict[str, tuple[list[str], list[str]]]) -> DocHeadings:
    """Real (unfenced) heading titles of each changed markdown file at
    `base_sha` and at HEAD, for prong 3 of `destructive_diff`.

    Read with `_run`, not `_run_git`: a file missing at one side (added,
    deleted, renamed away) is a normal non-zero `git show`, and its side is
    None — prong 3 then reads that side's `#` lines exactly as it did before
    this filter existed, so a read failure can never ADD a firing.
    """
    out: DocHeadings = {}
    for path in hunks:
        if Path(path).suffix.lower() not in _MARKDOWN_EXTS:
            continue
        sides: list[frozenset[str] | None] = []
        for rev in (base_sha, "HEAD"):
            rc, text = _run(["git", "show", f"{rev}:{path}"], cwd=repo_path,
                            timeout=60)
            sides.append(_unfenced_headings(text) if rc == 0 else None)
        out[path] = (sides[0], sides[1])
    return out


def _added_text_from_hunks(
    hunks: dict[str, tuple[list[str], list[str]]],
) -> str:
    """The `+` lines of a parsed diff, in diff order, as one blob."""
    added: list[str] = []
    for _path, (file_added, _deleted) in hunks.items():
        added.extend(file_added)
    return "\n".join(added)


def _diff_added_text(repo_path: Path, base_sha: str,
                     changed_files: list[str]) -> str:
    """Concatenate the `+` lines of `git diff base_sha HEAD -- *changed_files`.

    Used by apply_strict_gates to detect placeholder text in only newly-added
    content (so pre-existing placeholders elsewhere don't trigger the gate).
    Strips the leading `+` and skips `+++` file-header markers.
    """
    return _added_text_from_hunks(_diff_hunks(repo_path, base_sha, changed_files))


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
    # placeholder_diff / tool_output_echo) cannot pass; the gate detail
    # becomes the failure reason. Phase 0 Bug 2: apply_strict_gates was defined but never
    # invoked from grade_fixture, so destructive diffs and placeholder
    # stubs were silently credited.
    gate_block: tuple[str, str] | None = None
    if is_write_task and result.diff_produced:
        hunks = _diff_hunks(repo_path, base_sha, changed)
        added_text = _added_text_from_hunks(hunks)
        doc_headings = _doc_headings(repo_path, base_sha, hunks)
        triggered = apply_strict_gates(
            task_type=fixture.task_type,
            file_paths=changed,
            additions=result.diff_additions,
            deletions=result.diff_deletions,
            diff_added_text=added_text,
            diff_hunks=hunks,
            doc_headings=doc_headings,
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
        # implement-only, as maintain_suite.sdd has always specified — the
        # code fired it for every tests_pass task. (No bugfix fixture uses
        # tests_pass today, so this narrowing changes no current score.)
        vacuous, vac_detail = False, ""
        if passed and fixture.task_type == "implement":
            vacuous, vac_detail = check_vacuous_test(
                repo_path, base_sha, eo.get("command", ""), changed,
            )
            if not vacuous and vac_detail:
                result.expected_outcome_detail = f"{detail}\n\n[vacuous_test] {vac_detail}"
        if passed:
            if vacuous:
                result.expected_outcome_passed = False
                result.expected_outcome_detail = (
                    f"{detail}\n\n[vacuous_test gate] {vac_detail}"
                )
                result.gates_triggered.append({
                    "name": "vacuous_test",
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
                        "name": "orphan_file",
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
                    "name": "orphan_file",
                    "detail": orph_detail[:500],
                })
                earned_outcome = 0
    elif kind == "regex_absent":
        passed, detail, deleted = _check_regex_absent(
            repo_path, eo.get("pattern", ""), changed, base_sha=base_sha)
        if passed and deleted:
            # Deleting the file that held the pattern is not removing the
            # pattern from it.
            passed = False
            gate_detail = f"target file(s) deleted instead of edited: {deleted[:5]}"
            detail = f"{detail}\n\n[deleted_target gate] {gate_detail}"
            result.gates_triggered.append({"name": "deleted_target",
                                           "detail": gate_detail[:500]})
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
