"""Tests for src/luxe/pr.py — preflight, branch naming, test detection."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from luxe import pr as pr_mod
from luxe.pr import (
    CmdResult,
    DirtyTreeError,
    GhAuthError,
    NoMutationsError,
    PRConfig,
    PRError,
    assert_clean_tree,
    detect_test_command,
    is_dirty,
    plan_branch_name,
    slugify_goal,
)
from luxe.run_state import RunSpec


def _cfg() -> PRConfig:
    return PRConfig(
        test_commands=[
            {"command": "pytest -q", "markers": ["pyproject.toml", "pytest.ini"]},
            {"command": "npm test", "markers": ["package.json"]},
            {"command": "cargo test", "markers": ["Cargo.toml"]},
        ],
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("# repo\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


# --- slug / branch ----------------------------------------------------------

def test_slugify_goal_basic():
    # Splits on non-alphanumerics; hyphens become token separators.
    # "Fix the off-by-one in pagination" → 8 tokens, capped at max_words=6.
    assert slugify_goal("Fix the off-by-one in pagination") == "fix-the-off-by-one-in"


def test_slugify_goal_truncates_long():
    s = slugify_goal("review the whole authentication subsystem for issues")
    assert s.count("-") <= 5  # 6 words, 5 hyphens


def test_slugify_goal_handles_no_words():
    assert slugify_goal("!!!") == "goal"


def test_plan_branch_name_no_collision(git_repo: Path, monkeypatch):
    monkeypatch.setattr(pr_mod, "_branch_exists_local", lambda r, n: False)
    monkeypatch.setattr(pr_mod, "_branch_exists_remote", lambda r, n: False)
    name = plan_branch_name("bugfix", "fix the bug", git_repo, _cfg())
    assert name == "luxe/bugfix/fix-the-bug"


def test_plan_branch_name_with_collision(git_repo: Path, monkeypatch):
    taken = {"luxe/bugfix/fix-the-bug", "luxe/bugfix/fix-the-bug-2"}
    monkeypatch.setattr(pr_mod, "_branch_exists_local", lambda r, n: n in taken)
    monkeypatch.setattr(pr_mod, "_branch_exists_remote", lambda r, n: False)
    name = plan_branch_name("bugfix", "fix the bug", git_repo, _cfg())
    assert name == "luxe/bugfix/fix-the-bug-3"


# --- test detection ---------------------------------------------------------

def test_detect_test_command_python(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("")
    assert detect_test_command(tmp_path, _cfg()) == "pytest -q"


def test_detect_test_command_node(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}")
    assert detect_test_command(tmp_path, _cfg()) == "npm test"


def test_detect_test_command_first_match_wins(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "package.json").write_text("{}")
    # Python is first in the list
    assert detect_test_command(tmp_path, _cfg()) == "pytest -q"


def test_detect_test_command_none_matched(tmp_path: Path):
    assert detect_test_command(tmp_path, _cfg()) == ""


# --- dirty-tree -------------------------------------------------------------

def test_is_dirty_clean_tree(git_repo: Path):
    assert not is_dirty(git_repo)


def test_is_dirty_with_untracked(git_repo: Path):
    (git_repo / "new.txt").write_text("untracked")
    assert is_dirty(git_repo)


def test_assert_clean_tree_passes_when_clean(git_repo: Path):
    assert_clean_tree(git_repo, allow_dirty=False)


def test_assert_clean_tree_aborts_when_dirty(git_repo: Path):
    (git_repo / "new.txt").write_text("untracked")
    with pytest.raises(DirtyTreeError):
        assert_clean_tree(git_repo, allow_dirty=False)


def test_allow_dirty_requires_confirmation(git_repo: Path):
    (git_repo / "new.txt").write_text("untracked")
    with pytest.raises(DirtyTreeError):
        # No confirm_callback → not confirmed
        assert_clean_tree(git_repo, allow_dirty=True, confirm_callback=None)


def test_allow_dirty_with_confirm_yes(git_repo: Path):
    (git_repo / "new.txt").write_text("untracked")
    assert_clean_tree(git_repo, allow_dirty=True, confirm_callback=lambda: True)


def test_allow_dirty_with_confirm_no(git_repo: Path):
    (git_repo / "new.txt").write_text("untracked")
    with pytest.raises(DirtyTreeError):
        assert_clean_tree(git_repo, allow_dirty=True, confirm_callback=lambda: False)


# --- gh auth ----------------------------------------------------------------

def test_assert_gh_auth_missing(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(pr_mod, "_run", boom)
    with pytest.raises(GhAuthError):
        pr_mod.assert_gh_auth()


def test_assert_gh_auth_unauthed(monkeypatch):
    monkeypatch.setattr(pr_mod, "_run",
                        lambda cmd, cwd, env=None, timeout=None:
                        CmdResult(rc=1, stdout="", stderr="not authenticated"))
    with pytest.raises(GhAuthError):
        pr_mod.assert_gh_auth()


def test_assert_gh_auth_ok(monkeypatch):
    monkeypatch.setattr(pr_mod, "_run",
                        lambda cmd, cwd, env=None, timeout=None:
                        CmdResult(rc=0, stdout="Logged in", stderr=""))
    pr_mod.assert_gh_auth()
