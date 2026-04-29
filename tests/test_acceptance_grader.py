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


def _f(id_: str, kind: str, **eo) -> Fixture:
    return Fixture(id=id_, goal="g", task_type="bugfix",
                   expected_outcome={"kind": kind, **eo})


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
    (git_repo / "src" / "main.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "noop"], cwd=git_repo, check=True)
    fix = _f("f3", "regex_absent", pattern=r"TODO")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert r.expected_outcome_passed
    assert r.score == 5


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
    fix = _f("f5", "tests_pass", command="true")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert r.expected_outcome_passed


def test_tests_pass_command_fails(git_repo: Path):
    base = _base_sha(git_repo)
    fix = _f("f6", "tests_pass", command="false")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert not r.expected_outcome_passed
    assert "rc=1" in r.expected_outcome_detail


# --- manual_review --

def test_manual_review_awards_zero(git_repo: Path):
    base = _base_sha(git_repo)
    fix = _f("f7", "manual_review", criteria="judge by hand")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    assert r.expected_outcome_passed is None
    # PR (1) + manual_review (0) + zero unresolved (1) = 2
    assert r.score == 2


# --- citation impact --

def test_unresolved_citations_dock_one_point(git_repo: Path):
    base = _base_sha(git_repo)
    fix = _f("f8", "regex_absent", pattern=r"banana")
    r = grade_fixture(fix, git_repo, pr_url="x", pr_opened=True,
                      citations_unresolved=2, citations_total=5, base_sha=base)
    # PR (1) + outcome (3) + unresolved>0 (0) = 4
    assert r.score == 4
    assert r.passed   # passes the 4/5 threshold


# --- pr.py impact --

def test_no_pr_docks_one_point(git_repo: Path):
    base = _base_sha(git_repo)
    fix = _f("f9", "regex_absent", pattern=r"banana")
    r = grade_fixture(fix, git_repo, pr_url="", pr_opened=False,
                      citations_unresolved=0, citations_total=0, base_sha=base)
    # No PR (0) + outcome (3) + zero unresolved (1) = 4
    assert r.score == 4


# --- summary --

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
