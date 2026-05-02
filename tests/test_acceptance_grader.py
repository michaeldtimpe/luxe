"""Tests for benchmarks/maintain_suite/grade.py — the acceptance grader.

The grader is a pure scoring layer over already-collected run artefacts;
we don't actually invoke luxe in these tests. Each test seeds the grading
inputs (PR opened?, citations resolved?, expected_outcome check details)
and asserts the score / pass verdict.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from benchmarks.maintain_suite.grade import (
    Fixture,
    FixtureResult,
    _diff_added_text,
    _diff_shortstat,
    fixture_pass_threshold,
    grade_fixture,
    summarize,
)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def add(a, b): return a + b\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _base_sha(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


def _f(id_: str, kind: str, task_type: str = "bugfix", **eo) -> Fixture:
    return Fixture(id=id_, goal="g", task_type=task_type,
                   expected_outcome={"kind": kind, **eo})


def _make_diff(repo: Path, content: str = "# noop\n") -> None:
    """Commit a non-empty diff so write-task gating treats this as 'luxe edited'."""
    target = repo / "src" / "main.py"
    target.write_text(target.read_text() + content)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "noop diff for grading test"],
                   cwd=repo, check=True)


# --- threshold --

def test_fixture_pass_threshold():
    assert not fixture_pass_threshold(0)
    assert not fixture_pass_threshold(3)
    assert fixture_pass_threshold(4)
    assert fixture_pass_threshold(5)


# --- regex_present --

def test_regex_present_match(git_repo: Path):
    base = _base_sha(git_repo)
    (git_repo / "src" / "main.py").write_text("def add(a, b):\n    return a + b\nfrom typing import Any\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=git_repo, check=True)

    fix = _f("f1", "regex_present", pattern=r"from typing")
    r = grade_fixture(fix, git_repo, pr_url="https://...", pr_opened=True,
                      citations_unresolved=0, citations_total=2, base_sha=base)
    assert r.expected_outcome_passed
    assert r.score == 5  # PR + outcome + zero unresolved


def test_regex_present_no_match(git_repo: Path):
    base = _base_sha(git_repo)
    (git_repo / "src" / "main.py").write_text("def add(a, b):\n    return a + b\n# changed\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=git_repo, check=True)

    fix = _f("f2", "regex_present", pattern=r"banana")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=1, base_sha=base)
    assert not r.expected_outcome_passed
    # PR (1) + outcome miss (0) + zero unresolved (1) = 2
    assert r.score == 2


# --- regex_absent --

def test_regex_absent_clean(git_repo: Path):
    base = _base_sha(git_repo)
    _make_diff(git_repo)
    fix = _f("f3", "regex_absent", pattern=r"TODO")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert r.expected_outcome_passed
    assert r.score == 5
    assert r.diff_produced


def test_regex_absent_violated(git_repo: Path):
    base = _base_sha(git_repo)
    (git_repo / "src" / "main.py").write_text("def add(a, b):\n    return a + b  # TODO refactor\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "todo"], cwd=git_repo, check=True)
    fix = _f("f4", "regex_absent", pattern=r"TODO")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert not r.expected_outcome_passed


# --- tests_pass --

def test_tests_pass_command_succeeds(git_repo: Path):
    base = _base_sha(git_repo)
    _make_diff(git_repo)
    fix = _f("f5", "tests_pass", command="true")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert r.expected_outcome_passed


def test_tests_pass_command_fails(git_repo: Path):
    base = _base_sha(git_repo)
    _make_diff(git_repo)
    fix = _f("f6", "tests_pass", command="false")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert not r.expected_outcome_passed
    assert "rc=1" in r.expected_outcome_detail


# --- manual_review --

def test_manual_review_awards_zero(git_repo: Path):
    base = _base_sha(git_repo)
    _make_diff(git_repo)
    fix = _f("f7", "manual_review", criteria="judge by hand")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert r.expected_outcome_passed is None
    # PR (1) + manual_review (0) + zero unresolved (1) = 2
    assert r.score == 2


# --- citation impact --

def test_unresolved_citations_dock_one_point(git_repo: Path):
    base = _base_sha(git_repo)
    _make_diff(git_repo)
    fix = _f("f8", "regex_absent", pattern=r"banana")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=2, citations_total=5, base_sha=base)
    # PR (1) + outcome (3) + unresolved>0 (0) = 4
    assert r.score == 4
    assert r.passed   # passes the 4/5 threshold


# --- pr.py impact --

def test_no_pr_docks_one_point(git_repo: Path):
    base = _base_sha(git_repo)
    _make_diff(git_repo)
    fix = _f("f9", "regex_absent", pattern=r"banana")
    r = grade_fixture(fix, git_repo, pr_url="", pr_opened=False,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    # No PR (0) + outcome (3) + zero unresolved (1) = 4
    assert r.score == 4


# --- diff-gating fix (the false-positive bug from neon-rain run) --

def test_write_task_no_diff_refuses_outcome_credit(git_repo: Path):
    """The bug we hit on neon-rain's first run: tests_pass on UNCHANGED code
    earned 3 outcome points. After fix, write tasks with no diff get 0."""
    base = _base_sha(git_repo)
    fix = _f("nx", "tests_pass", task_type="bugfix", command="true")
    r = grade_fixture(fix, git_repo, pr_url="", pr_opened=False,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    # noPR(0) + outcome refused(0) + cite(1) = 1; not 4 like the false positive
    assert r.score == 1
    assert not r.diff_produced
    assert not r.expected_outcome_passed
    assert "no diff" in r.expected_outcome_detail
    # And it should not pass overall.
    assert not r.passed


def test_write_task_no_diff_refuses_regex_credit_too(git_repo: Path):
    base = _base_sha(git_repo)
    fix = _f("nx2", "regex_present", task_type="implement",
             pattern=r"anything")
    r = grade_fixture(fix, git_repo, pr_url="", pr_opened=False,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert r.score == 1
    assert not r.expected_outcome_passed


@pytest.mark.parametrize("task_type", ["review", "summarize"])
def test_read_only_task_no_diff_still_credits_outcome(git_repo: Path,
                                                       task_type: str):
    """Read-only tasks (review/summarize) legitimately produce no diff.
    The grader must not gate them on diff_produced."""
    base = _base_sha(git_repo)
    fix = _f("rx", "regex_absent", task_type=task_type, pattern=r"banana")
    r = grade_fixture(fix, git_repo, pr_url="", pr_opened=False,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    # Read-only: no diff is fine; outcome credited normally → 0+3+1 = 4
    assert r.score == 4
    assert r.expected_outcome_passed


def test_criteria_breakdown_records_each_check(git_repo: Path):
    base = _base_sha(git_repo)
    _make_diff(git_repo)
    fix = _f("cx", "tests_pass", command="true")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=2, base_sha=base)
    names = [c["criterion"] for c in r.criteria_breakdown]
    assert "pr_opened" in names
    assert any("expected_outcome" in n for n in names)
    assert "citations_resolved" in names
    weights = [c["weight"] for c in r.criteria_breakdown]
    assert sum(weights) == 5


# --- summary --

# --- regex_present failure-message priority (P0.3) --
# When both min_matches and min_added_lines fail, report the more informative
# floor (matches deficit > lines deficit). Before this reorder, the error
# message mis-attributed the lpe-rope-calc-document-typing baseline failure to
# "only 4 added lines" when the deeper issue was that the model only typed 3
# of N functions (min_matches: 4 would have caught it regardless).

def _setup_repo_with_n_added_lines(repo: Path, n_lines: int,
                                    matching_lines: int = 0,
                                    pattern_text: str = "MATCH") -> str:
    """Create a commit on top of git_repo with exactly n_lines added lines,
    of which `matching_lines` contain `pattern_text`. Returns base_sha."""
    base = _base_sha(repo)
    target = repo / "src" / "main.py"
    body = []
    for i in range(matching_lines):
        body.append(f"# {pattern_text} {i}")
    for i in range(n_lines - matching_lines):
        body.append(f"# benign line {i}")
    target.write_text(target.read_text() + "\n" + "\n".join(body) + "\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "n added"], cwd=repo, check=True)
    return base


def test_regex_present_reports_match_deficit_when_both_floors_fail(git_repo: Path):
    """4 added lines + 3 matches against min_matches=4, min_added_lines=6:
    both floors fail. Failure detail must mention matches, not lines."""
    base = _setup_repo_with_n_added_lines(git_repo, n_lines=4, matching_lines=3)
    fix = _f("rg1", "regex_present", task_type="document",
             pattern=r"MATCH", min_matches=4, min_added_lines=6)
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert not r.expected_outcome_passed
    detail = r.expected_outcome_detail
    assert "matched" in detail.lower(), \
        f"expected match-count message, got: {detail!r}"
    assert "needed ≥4" in detail
    assert "added lines, need" not in detail, \
        f"min_added_lines message leaked: {detail!r}"


def test_regex_present_reports_match_deficit_when_only_matches_fail(git_repo: Path):
    """8 added lines + 1 match against min_matches=4, min_added_lines=6:
    lines floor passes, matches floor fails. Must report matches deficit."""
    base = _setup_repo_with_n_added_lines(git_repo, n_lines=8, matching_lines=1)
    fix = _f("rg2", "regex_present", task_type="document",
             pattern=r"MATCH", min_matches=4, min_added_lines=6)
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert not r.expected_outcome_passed
    detail = r.expected_outcome_detail
    assert "needed ≥4" in detail
    assert "matched 1×" in detail or "1×" in detail


def test_regex_present_passes_when_both_floors_cleared(git_repo: Path):
    """8 added lines + 1 match against min_matches=1, min_added_lines=6:
    both floors satisfied. Must pass — confirms the reorder didn't break
    the OR semantics."""
    base = _setup_repo_with_n_added_lines(git_repo, n_lines=8, matching_lines=1)
    fix = _f("rg3", "regex_present", task_type="document",
             pattern=r"MATCH", min_matches=1, min_added_lines=6)
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert r.expected_outcome_passed, \
        f"expected pass with both floors cleared, got: {r.expected_outcome_detail!r}"
    assert r.score == 5


# --- orphan-file gate --

def _commit_added_files(repo: Path, files: dict[str, str], message: str) -> None:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def _seed_js_repo(repo: Path) -> None:
    """Replace the default Python repo state with a JS project that
    already contains an HtmlInputHandler.js, mirroring the neon-rain shape."""
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "input").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "input" / "HtmlInputHandler.js").write_text(
        "export class HtmlInputHandler {\n  constructor() {}\n}\n"
    )
    (repo / "src" / "Game.js").write_text(
        "import { HtmlInputHandler } from './input/HtmlInputHandler.js';\n"
        "export class Game { constructor() { this.input = new HtmlInputHandler(); } }\n"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "js project base"],
                   cwd=repo, check=True)


def test_orphan_file_gate_flags_duplicate_stem_in_same_dir(git_repo: Path):
    """Reproduces the granite-3b neon-rain exploit: model adds
    HtmlInputHandler.ts next to existing HtmlInputHandler.js. Tests pass
    against the unchanged JS implementation; the new TS file is orphan."""
    _seed_js_repo(git_repo)
    base = _base_sha(git_repo)
    _commit_added_files(git_repo, {
        "src/input/HtmlInputHandler.ts":
            "export default class HtmlInputHandler {\n  bindEvents() {}\n}\n"
    }, "orphan ts duplicate")
    fix = _f("orph1", "tests_pass", task_type="implement", command="true")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert not r.expected_outcome_passed
    assert any(g["gate"] == "orphan_file" for g in r.gates_triggered)
    assert "duplicates existing" in r.expected_outcome_detail


def test_orphan_file_gate_flags_unreferenced_new_source(git_repo: Path):
    """Model adds a brand-new source file that nothing imports."""
    base = _base_sha(git_repo)
    _commit_added_files(git_repo, {
        "src/healthcheck.py":
            "def health_endpoint():\n    return {'status': 'ok'}\n"
    }, "unwired new file")
    fix = _f("orph2", "regex_present", task_type="implement",
             pattern=r"def health_endpoint")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert not r.expected_outcome_passed
    assert any(g["gate"] == "orphan_file" for g in r.gates_triggered)


def test_orphan_file_gate_does_not_fire_when_new_file_is_imported(git_repo: Path):
    """A new source file that's wired into existing code is NOT orphan."""
    base = _base_sha(git_repo)
    # main.py already exists from the git_repo fixture; modify it to import
    # the new module and add the new module itself.
    main = git_repo / "src" / "main.py"
    main.write_text(main.read_text()
                    + "from src.helper import compute\n")
    _commit_added_files(git_repo, {
        "src/helper.py": "def compute(x): return x * 2\n",
    }, "new module wired in")
    fix = _f("orph3", "regex_present", task_type="implement",
             pattern=r"def compute")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert r.expected_outcome_passed
    assert not any(g["gate"] == "orphan_file" for g in r.gates_triggered)


def test_orphan_file_gate_skips_document_tasks(git_repo: Path):
    """document tasks legitimately add standalone files (e.g. CONFIG.md,
    or a brand-new module that's just a code reference). The gate only
    applies to implement/bugfix."""
    base = _base_sha(git_repo)
    _commit_added_files(git_repo, {
        "src/standalone_doc.py":
            '"""Module-level docstring referenced from README only."""\n'
    }, "doc-task addition")
    fix = _f("orph4", "regex_present", task_type="document",
             pattern=r"docstring")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    # Doc tasks pass even when the new file is technically an orphan.
    assert r.expected_outcome_passed
    assert not any(g["gate"] == "orphan_file" for g in r.gates_triggered)


def test_orphan_file_gate_skips_non_source_additions(git_repo: Path):
    """Implement task that adds a markdown or yaml is not a source-orphan."""
    base = _base_sha(git_repo)
    _commit_added_files(git_repo, {
        "NOTES.md": "# Implementation notes\n\nSee src/main.py.\n",
        "src/main.py": (git_repo / "src" / "main.py").read_text()
                       + "def health(): return 'ok'\n",
    }, "real impl + notes file")
    fix = _f("orph5", "regex_present", task_type="implement",
             pattern=r"def health")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert r.expected_outcome_passed
    assert not any(g["gate"] == "orphan_file" for g in r.gates_triggered)


def test_summarize_counts_pass_fail():
    results = [
        FixtureResult(fixture_id=f"f{i}", score=5) for i in range(8)
    ] + [
        FixtureResult(fixture_id="fa", score=2),
        FixtureResult(fixture_id="fb", score=0, error="boom"),
    ]
    s = summarize(results)
    assert s["fixtures"] == 10
    assert s["passed"] == 8
    assert s["errored"] == 1
    assert s["v1_release_gate"]


def test_v1_release_gate_requires_8_of_10():
    seven = [FixtureResult(fixture_id=f"f{i}", score=5) for i in range(7)] + \
            [FixtureResult(fixture_id="x", score=2) for _ in range(3)]
    s = summarize(seven)
    assert s["passed"] == 7
    assert not s["v1_release_gate"]


def test_v1_release_gate_requires_at_least_10_total():
    nine_all_pass = [FixtureResult(fixture_id=f"f{i}", score=5) for i in range(9)]
    s = summarize(nine_all_pass)
    assert s["passed"] == 9
    assert not s["v1_release_gate"]


# --- multi-variant v1_release_gate (Phase 1 prompt-shaping bug) --

def test_v1_release_gate_multi_variant_no_cell_cleared():
    """The Phase 1 prompt-shaping sweep (60 runs) had 33 total passes,
    which crossed the old `passed >= 8` flat check, producing a false
    "v1 release: YES" verdict despite no individual cell hitting 8/10.
    The fix: when `per_variant` is passed, gate is True iff some cell
    has ≥8 fresh passes."""
    # 6 cells × 10 fixtures, each cell with 4-7 passes (none clear 8).
    cell_a = [FixtureResult(fixture_id=f"a{i}", score=5) for i in range(7)] + \
             [FixtureResult(fixture_id=f"a{i}", score=2) for i in range(3)]
    cell_b = [FixtureResult(fixture_id=f"b{i}", score=5) for i in range(6)] + \
             [FixtureResult(fixture_id=f"b{i}", score=2) for i in range(4)]
    cell_c = [FixtureResult(fixture_id=f"c{i}", score=5) for i in range(4)] + \
             [FixtureResult(fixture_id=f"c{i}", score=2) for i in range(6)]
    flat = cell_a + cell_b + cell_c  # 17 total passes — old logic says YES
    s = summarize(flat, per_variant={"a": cell_a, "b": cell_b, "c": cell_c})
    assert s["passed"] == 17  # flat count is correct
    # But the per-cell gate must be NO — no cell hit 8.
    assert not s["v1_release_gate"]
    assert s["v1_release_gate_per_variant"] == {"a": False, "b": False, "c": False}


def test_v1_release_gate_multi_variant_one_cell_cleared():
    """If ONE cell hits 8/10, the multi-variant gate is YES (we have a
    promotable variant). The total-passes flat check is irrelevant."""
    winner = [FixtureResult(fixture_id=f"w{i}", score=5) for i in range(8)] + \
             [FixtureResult(fixture_id=f"w{i}", score=2) for i in range(2)]
    loser = [FixtureResult(fixture_id=f"l{i}", score=2) for i in range(10)]
    flat = winner + loser  # 8 total — same as old single-variant gate
    s = summarize(flat, per_variant={"winner": winner, "loser": loser})
    assert s["v1_release_gate"]
    assert s["v1_release_gate_per_variant"] == {"winner": True, "loser": False}


def test_v1_release_gate_single_variant_unchanged():
    """No per_variant arg → uses the original flat threshold. Existing
    callers (older single-variant runs) keep working unchanged."""
    ten_all_pass = [FixtureResult(fixture_id=f"f{i}", score=5) for i in range(10)]
    s = summarize(ten_all_pass)
    assert s["v1_release_gate"]
    # Empty per-variant dict in the output for back-compat.
    assert s["v1_release_gate_per_variant"] == {}


# --- diff stat helpers (Phase 0 grader fix; Bug 1) --

def test_diff_shortstat_basic(git_repo: Path):
    """Known +5/-3 diff should parse to (5, 3)."""
    base = _base_sha(git_repo)
    # +5 lines: extend main.py with 5 new lines.
    target = git_repo / "src" / "main.py"
    target.write_text("def add(a, b):\n    return a + b\n"  # was 1 line; now overwriting
                      "x = 1\ny = 2\nz = 3\nq = 4\nr = 5\n")
    # That replaced 1 line with 7 → +7/-1. Trim to a clean +5/-3 by adjusting.
    target.write_text("a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n")  # 5 new lines
    # Note: original had `def add(a, b): return a + b\n` (1 line). Replacing
    # with 5 lines → +5/-1. To get +5/-3 we need to start with 3 lines.
    # Simpler: rewrite the test using a controlled multi-line base.
    subprocess.run(["git", "reset", "--hard", "-q", base], cwd=git_repo, check=True)
    target.write_text("L1\nL2\nL3\n")  # 3 lines
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "3-line base"],
                   cwd=git_repo, check=True)
    new_base = _base_sha(git_repo)
    target.write_text("A\nB\nC\nD\nE\n")  # delete 3, add 5 → +5/-3
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "delta"],
                   cwd=git_repo, check=True)

    adds, dels = _diff_shortstat(git_repo, new_base)
    assert adds == 5
    assert dels == 3


def test_diff_shortstat_pure_addition(git_repo: Path):
    """`--shortstat` omits the deletions clause when there are no deletions.
    The parser must tolerate that and return (N, 0), not crash on missing match."""
    base = _base_sha(git_repo)
    (git_repo / "NEW.txt").write_text("a\nb\nc\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add file"],
                   cwd=git_repo, check=True)

    adds, dels = _diff_shortstat(git_repo, base)
    assert adds == 3
    assert dels == 0


def test_diff_shortstat_empty(git_repo: Path):
    """No diff vs base → (0, 0)."""
    base = _base_sha(git_repo)
    adds, dels = _diff_shortstat(git_repo, base)
    assert adds == 0
    assert dels == 0


def test_diff_shortstat_no_base_sha(git_repo: Path):
    """Empty base_sha → (0, 0); guards against accidentally diffing whole repo."""
    adds, dels = _diff_shortstat(git_repo, "")
    assert adds == 0
    assert dels == 0


def test_diff_added_text_strips_markers(git_repo: Path):
    """`+++ b/file` headers must be skipped; the leading `+` on real added
    lines must be stripped from the body."""
    base = _base_sha(git_repo)
    target = git_repo / "src" / "main.py"
    target.write_text("def add(a, b): return a + b\nNEW_LINE_ONE\nNEW_LINE_TWO\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "delta"],
                   cwd=git_repo, check=True)

    text = _diff_added_text(git_repo, base, ["src/main.py"])
    # The body of the added lines should appear without `+` prefix; the
    # `+++ b/src/main.py` header should NOT.
    assert "NEW_LINE_ONE" in text
    assert "NEW_LINE_TWO" in text
    assert "+++ b/src/main.py" not in text
    # Leading `+` stripped → body shouldn't start with `+`.
    for line in text.splitlines():
        assert not line.startswith("+++")


def test_diff_added_text_empty_when_no_changed_files(git_repo: Path):
    """No changed files → empty string (not crash on empty *args to git)."""
    base = _base_sha(git_repo)
    text = _diff_added_text(git_repo, base, [])
    assert text == ""


def test_grade_fixture_records_shortstat(git_repo: Path):
    """grade_fixture must populate result.diff_additions / diff_deletions
    from `git diff base_sha HEAD --shortstat`. Phase 0 Bug 1 invariant.
    """
    base = _base_sha(git_repo)
    target = git_repo / "src" / "main.py"
    # Original main.py is `def add(a, b): return a + b\n` (1 line).
    # Replace with 4 new lines → +4/-1.
    target.write_text("X1\nX2\nX3\nX4\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "delta"], cwd=git_repo, check=True)

    fix = _f("f1", "regex_present", pattern=r"X\d")
    r = grade_fixture(fix, git_repo, pr_url="https://...", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert r.diff_additions == 4
    assert r.diff_deletions == 1


def test_grade_fixture_shortstat_zero_when_no_diff(git_repo: Path):
    """No diff → diff_additions and diff_deletions stay 0 (no spurious calls)."""
    base = _base_sha(git_repo)
    fix = _f("f1", "regex_present", pattern=r"anything")
    r = grade_fixture(fix, git_repo, pr_url="", pr_opened=False,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert r.diff_additions == 0
    assert r.diff_deletions == 0


# --- strict gates wired into grade_fixture (Phase 0 Bug 2) --

def test_destructive_diff_blocks_write_task_pass(git_repo: Path):
    """Reproduce the gut-the-codebase pattern (deletions/additions ≥ 5×,
    deletions ≥ 30) on a write task; assert outcome=False AND gates_triggered
    contains destructive_diff. The committed diff would otherwise pass the
    regex check on the small added lines."""
    base = _base_sha(git_repo)
    # Seed a large file to delete from.
    big = git_repo / "src" / "big.py"
    big.write_text("\n".join(f"line_{i}" for i in range(50)) + "\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed big file"],
                   cwd=git_repo, check=True)
    new_base = _base_sha(git_repo)

    # Now: delete most of big.py, add a small new function in main.py
    # that satisfies the regex_present pattern. -50/+5 ratio.
    big.write_text("# all gone\n")  # 1 line; -50/+1 in big.py
    target = git_repo / "src" / "main.py"
    target.write_text(target.read_text() + "def new_thing(): return 42\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "destructive"],
                   cwd=git_repo, check=True)

    fix = _f("f-destruct", "regex_present", task_type="implement",
             pattern=r"def new_thing")
    r = grade_fixture(fix, git_repo, pr_url="https://...", pr_opened=True,
                      citations_unresolved=0, citations_total=0,
                      base_sha=new_base)
    assert r.expected_outcome_passed is False
    gate_names = [g.get("name", g.get("gate", "")) for g in r.gates_triggered]
    assert "destructive_diff" in gate_names
    # Score caps at 2 (PR + citations); outcome is blocked.
    assert r.score == 2


def test_placeholder_diff_blocks_write_task_pass(git_repo: Path):
    """A diff containing placeholder text (e.g. 'your X code here') must
    block outcome credit on write tasks."""
    base = _base_sha(git_repo)
    target = git_repo / "src" / "main.py"
    target.write_text(target.read_text()
                      + "def widget():\n    # your widget code here\n    pass\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "stub"], cwd=git_repo, check=True)

    fix = _f("f-placeholder", "regex_present", task_type="implement",
             pattern=r"def widget")
    r = grade_fixture(fix, git_repo, pr_url="https://...", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert r.expected_outcome_passed is False
    gate_names = [g.get("name", g.get("gate", "")) for g in r.gates_triggered]
    assert "placeholder_diff" in gate_names


def test_clean_write_task_gates_dont_fire(git_repo: Path):
    """A clean, non-destructive, non-placeholder diff should pass without
    triggering any pre-outcome gate. Sanity check that we haven't broken
    happy-path passes."""
    base = _base_sha(git_repo)
    target = git_repo / "src" / "main.py"
    target.write_text(target.read_text()
                      + "def helpful():\n    return 'work done'\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "clean"], cwd=git_repo, check=True)

    fix = _f("f-clean", "regex_present", task_type="implement",
             pattern=r"def helpful")
    r = grade_fixture(fix, git_repo, pr_url="https://...", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert r.expected_outcome_passed is True
    gate_names = [g.get("name", g.get("gate", "")) for g in r.gates_triggered]
    assert "destructive_diff" not in gate_names
    assert "placeholder_diff" not in gate_names
    assert "role_name_leak" not in gate_names


def test_read_only_task_gates_skip(git_repo: Path):
    """Read-mode tasks (review, summarize) skip strict gates entirely; a
    diff with destructive shape should not be penalized for a task type
    that wasn't supposed to produce a diff in the first place."""
    base = _base_sha(git_repo)
    big = git_repo / "src" / "big.py"
    big.write_text("\n".join(f"line_{i}" for i in range(50)) + "\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=git_repo, check=True)
    new_base = _base_sha(git_repo)
    big.write_text("# nope\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "destructive on review task"],
                   cwd=git_repo, check=True)

    # Review task — should NOT block on destructive_diff, since the task
    # type indicates the agent wasn't supposed to be writing in the first
    # place; that's an upstream issue for a separate gate.
    fix = _f("f-review", "manual_review", task_type="review",
             criteria="anything goes")
    r = grade_fixture(fix, git_repo, pr_url="", pr_opened=False,
                      citations_unresolved=0, citations_total=0,
                      base_sha=new_base)
    gate_names = [g.get("name", g.get("gate", "")) for g in r.gates_triggered]
    assert "destructive_diff" not in gate_names
