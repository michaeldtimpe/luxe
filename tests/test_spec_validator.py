"""Tests for src/luxe/spec_validator.py — predicate evaluation.

Builds tiny git repos in tmp_path to exercise the diff-walking and
predicate logic without a real fixture cache. Each test is isolated.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe.spec import Requirement, Spec
from luxe.spec_validator import (
    RequirementResult,
    ValidationResult,
    _added_lines_from_diff,
    format_unsatisfied_for_reprompt,
    validate,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return proc.stdout


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a minimal git repo with a single committed file. Return
    (repo_path, base_sha) so tests can diff later edits against the
    initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("# initial\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()
    return repo, base_sha


# --- _added_lines_from_diff -----------------------------------------------


class TestAddedLinesFromDiff:
    def test_no_changes_returns_empty(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        assert _added_lines_from_diff(repo, base) == []

    def test_modified_tracked_file(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        (repo / "README.md").write_text("# initial\n\nnew prose line\n")
        added = _added_lines_from_diff(repo, base)
        bodies = [body for _, body in added]
        assert "" in bodies          # the blank line
        assert "new prose line" in bodies

    def test_new_untracked_file_visible_via_intent_to_add(self, tmp_path):
        """Critical: matches the cli.py bug-fix at v1.3.1. New files
        created by write_file are untracked; without `git add -N`, they
        do not appear in `git diff <base_sha>`. The validator's diff
        helper must surface them."""
        repo, base = _init_repo(tmp_path)
        (repo / "CONFIG.md").write_text("first line\nsecond line\n")
        added = _added_lines_from_diff(repo, base)
        bodies = [body for _, body in added]
        assert "first line" in bodies
        assert "second line" in bodies
        files = {fname for fname, _ in added}
        assert any("CONFIG.md" in f for f in files)

    def test_empty_base_sha_returns_empty(self, tmp_path):
        repo, _ = _init_repo(tmp_path)
        (repo / "x.md").write_text("hi\n")
        assert _added_lines_from_diff(repo, "") == []


# --- regex_present --------------------------------------------------------


class TestRegexPresent:
    def test_pattern_matches_pass(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        (repo / "config.py").write_text(
            "import os\nVAR = os.getenv('FOO')\nVAR2 = os.getenv('BAR')\n"
        )
        spec = Spec(
            goal="document env vars",
            requirements=[Requirement(
                id="R1", must="x", done_when="y",
                kind="regex_present",
                pattern=r"os\.getenv",
                min_matches=2,
            )],
        )
        result = validate(spec, repo, base)
        assert result.all_satisfied
        assert "matched in 2 added lines" in result.results[0].detail

    def test_pattern_below_min_matches_fail(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        (repo / "config.py").write_text("VAR = os.getenv('FOO')\n")
        spec = Spec(
            goal="x",
            requirements=[Requirement(
                id="R1", must="x", done_when="y",
                kind="regex_present",
                pattern=r"os\.getenv",
                min_matches=3,
            )],
        )
        result = validate(spec, repo, base)
        assert not result.all_satisfied
        assert "matched 1×" in result.results[0].detail
        assert "needed ≥3" in result.results[0].detail

    def test_pattern_no_matches_fail(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        (repo / "irrelevant.txt").write_text("hello world\n")
        spec = Spec(
            goal="x",
            requirements=[Requirement(
                id="R1", must="x", done_when="y",
                kind="regex_present",
                pattern=r"os\.getenv",
                min_matches=1,
            )],
        )
        result = validate(spec, repo, base)
        assert not result.all_satisfied

    def test_match_finds_new_file_content(self, tmp_path):
        """Specifically tests the new-file path (regression for
        the v1.3.1 _diff_against_base bug)."""
        repo, base = _init_repo(tmp_path)
        (repo / "CONFIG.md").write_text(
            "## Env Vars\n- FOO_BAR_BAZ: thing\n- QUX_VAR: thing\n"
        )
        spec = Spec(
            goal="x",
            requirements=[Requirement(
                id="R1", must="x", done_when="y",
                kind="regex_present",
                pattern=r"\b[A-Z]+_[A-Z_]+\b",
                min_matches=2,
            )],
        )
        result = validate(spec, repo, base)
        assert result.all_satisfied


# --- regex_absent ---------------------------------------------------------


class TestRegexAbsent:
    def test_pattern_absent_pass(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        (repo / "ok.py").write_text("def real():\n    return 42\n")
        spec = Spec(
            goal="x",
            requirements=[Requirement(
                id="R1", must="no TODOs", done_when="pattern absent",
                kind="regex_absent",
                pattern=r"TODO",
            )],
        )
        result = validate(spec, repo, base)
        assert result.all_satisfied
        assert "absent" in result.results[0].detail

    def test_pattern_present_fails_with_first_match(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        (repo / "bad.py").write_text("def x():\n    pass  # TODO: implement\n")
        spec = Spec(
            goal="x",
            requirements=[Requirement(
                id="R1", must="no TODOs", done_when="pattern absent",
                kind="regex_absent",
                pattern=r"TODO",
            )],
        )
        result = validate(spec, repo, base)
        assert not result.all_satisfied
        assert "forbidden pattern matched" in result.results[0].detail
        assert "TODO" in result.results[0].detail


# --- tests_pass -----------------------------------------------------------


class TestTestsPass:
    def test_command_exit_zero_passes(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        spec = Spec(
            goal="x",
            requirements=[Requirement(
                id="R1", must="ls works", done_when="exit 0",
                kind="tests_pass",
                command="true",
            )],
        )
        result = validate(spec, repo, base)
        assert result.all_satisfied

    def test_command_exit_nonzero_fails(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        spec = Spec(
            goal="x",
            requirements=[Requirement(
                id="R1", must="x", done_when="y",
                kind="tests_pass",
                command="false",
            )],
        )
        result = validate(spec, repo, base)
        assert not result.all_satisfied
        assert "rc=1" in result.results[0].detail


# --- ast_query / manual stubs ---------------------------------------------


class TestStubs:
    def test_ast_query_reports_unsatisfied_with_notice(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        spec = Spec(
            goal="x",
            requirements=[Requirement(
                id="R1", must="x", done_when="some ast pattern",
                kind="ast_query",
                pattern="some_query",
            )],
        )
        result = validate(spec, repo, base)
        assert not result.all_satisfied
        assert "not yet implemented" in result.results[0].detail

    def test_manual_always_unsatisfied(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        spec = Spec(
            goal="x",
            requirements=[Requirement(
                id="R1", must="x", done_when="reviewer judgement",
                kind="manual",
            )],
        )
        result = validate(spec, repo, base)
        assert not result.all_satisfied
        assert "manual review" in result.results[0].detail


# --- aggregate ValidationResult -------------------------------------------


class TestValidationResult:
    def test_unsatisfied_filters_to_failures(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        (repo / "x.py").write_text("a = 1\n")
        spec = Spec(
            goal="x",
            requirements=[
                Requirement(
                    id="R1", must="x", done_when="y",
                    kind="regex_present", pattern=r"a = 1",
                ),
                Requirement(
                    id="R2", must="x", done_when="y",
                    kind="regex_present", pattern=r"never_present",
                ),
                Requirement(
                    id="R3", must="x", done_when="y",
                    kind="regex_absent", pattern=r"FORBIDDEN",
                ),
            ],
        )
        result = validate(spec, repo, base)
        assert not result.all_satisfied
        assert len(result.unsatisfied) == 1
        assert result.unsatisfied[0].requirement.id == "R2"

    def test_all_satisfied_when_every_predicate_passes(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        (repo / "x.py").write_text("a = 1\n")
        spec = Spec(
            goal="x",
            requirements=[Requirement(
                id="R1", must="x", done_when="y",
                kind="regex_present", pattern=r"a = 1",
            )],
        )
        result = validate(spec, repo, base)
        assert result.all_satisfied
        assert result.unsatisfied == []

    def test_empty_requirements_trivially_satisfied(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        spec = Spec(goal="just prose")
        result = validate(spec, repo, base)
        assert result.all_satisfied
        assert result.results == []

    def test_no_early_exit_evaluates_all_requirements(self, tmp_path):
        """All requirements evaluate even when an earlier one fails —
        synthesizer needs the full set of unsatisfied items per pass."""
        repo, base = _init_repo(tmp_path)
        spec = Spec(
            goal="x",
            requirements=[
                Requirement(
                    id="R1", must="x", done_when="y",
                    kind="regex_present", pattern=r"never1",
                ),
                Requirement(
                    id="R2", must="x", done_when="y",
                    kind="regex_present", pattern=r"never2",
                ),
                Requirement(
                    id="R3", must="x", done_when="y",
                    kind="regex_absent", pattern=r"never3",
                ),
            ],
        )
        result = validate(spec, repo, base)
        assert len(result.results) == 3  # R3 still evaluated despite R1/R2 failing


class TestFormatUnsatisfiedForReprompt:
    def test_all_satisfied_returns_empty(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        (repo / "x.py").write_text("a = 1\n")
        spec = Spec(
            goal="x",
            requirements=[Requirement(
                id="R1", must="x", done_when="y",
                kind="regex_present", pattern=r"a = 1",
            )],
        )
        result = validate(spec, repo, base)
        assert result.all_satisfied
        assert format_unsatisfied_for_reprompt(result) == ""

    def test_unsatisfied_includes_id_must_done_when_detail(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        spec = Spec(
            goal="x",
            requirements=[Requirement(
                id="R2",
                must="Add a module docstring",
                done_when="regex matches at file head",
                kind="regex_present",
                pattern=r"^never_present",
            )],
        )
        result = validate(spec, repo, base)
        out = format_unsatisfied_for_reprompt(result)
        assert "R2: Add a module docstring" in out
        assert "Graded by: regex matches at file head" in out
        assert "Current state:" in out
        assert "edit_file" in out  # imperative tail

    def test_renders_only_unsatisfied_requirements(self, tmp_path):
        repo, base = _init_repo(tmp_path)
        (repo / "x.py").write_text("good_token\n")
        spec = Spec(
            goal="x",
            requirements=[
                Requirement(
                    id="R1", must="good token present",
                    done_when="x", kind="regex_present",
                    pattern=r"good_token",
                ),
                Requirement(
                    id="R2", must="missing token present",
                    done_when="x", kind="regex_present",
                    pattern=r"never_appears",
                ),
            ],
        )
        result = validate(spec, repo, base)
        out = format_unsatisfied_for_reprompt(result)
        # R1 satisfied — should NOT appear in reprompt
        assert "R1:" not in out
        assert "R2:" in out
        assert "missing token present" in out
