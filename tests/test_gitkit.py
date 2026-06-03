"""Tests for the gitkit read-only repo-analysis toolkit.

Covers the deterministic, non-model pieces: git-health gathering (incl. the
empty-repo edge case), graceful gh degradation, report persistence, and the
CLI alias resolution. The agent pass itself (run_single) is exercised
end-to-end manually against oMLX, not here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe.gitkit import health, store


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    (repo / "main.py").write_text("def f():\n    return 1\n")
    (repo / "requirements.txt").write_text("requests\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial commit")
    (repo / "main.py").write_text("def f():\n    return 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tweak f")
    return repo


def test_gather_repo_health_reports_commits_and_size(git_repo: Path):
    block = health.gather_repo_health(git_repo)
    assert block.startswith("<repo_health>")
    assert block.rstrip().endswith("</repo_health>")
    assert "Total commits: 2" in block
    assert "Distinct authors: 1" in block
    assert "Files:" in block and "Tracked LOC:" in block
    assert "requirements.txt" in block  # manifest detected
    assert "tweak f" in block  # recent subject


def test_gather_repo_health_empty_repo_does_not_crash(tmp_path: Path):
    repo = tmp_path / "blank"
    repo.mkdir()
    _git(repo, "init", "-q")
    block = health.gather_repo_health(repo)
    assert "blank repository" in block.lower()
    assert block.startswith("<repo_health>")


def test_current_head_blank_repo_is_empty(tmp_path: Path):
    repo = tmp_path / "blank"
    repo.mkdir()
    _git(repo, "init", "-q")
    assert health.current_head(repo) == ""


def test_github_metadata_non_github_remote(git_repo: Path):
    block = health.gather_github_metadata(git_repo)
    assert "GitHub metadata unavailable" in block
    assert block.startswith("<github_metadata>")


def test_github_metadata_degrades_when_gh_unavailable(git_repo: Path, monkeypatch):
    # GitHub remote present, but gh fails (missing / unauthenticated / timeout).
    _git(git_repo, "remote", "add", "origin", "https://github.com/acme/widget.git")

    def _fake_gh(args, repo_path, timeout=30):
        return False, "gh CLI not installed"

    monkeypatch.setattr(health, "_run_gh", _fake_gh)
    block = health.gather_github_metadata(git_repo)
    assert "GitHub metadata unavailable (gh CLI not installed)" in block
    assert "local git only" in block


def test_github_metadata_uses_gh_when_available(git_repo: Path, monkeypatch):
    _git(git_repo, "remote", "add", "origin", "https://github.com/acme/widget.git")

    def _fake_gh(args, repo_path, timeout=30):
        if args[:2] == ["repo", "view"]:
            return True, '{"nameWithOwner":"acme/widget","stargazerCount":42}'
        if args[:2] == ["pr", "list"]:
            return True, "[]"
        return True, ""

    monkeypatch.setattr(health, "_run_gh", _fake_gh)
    block = health.gather_github_metadata(git_repo)
    assert "acme/widget" in block
    assert "42" in block


def test_save_report_writes_frontmatter_and_path(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = store.save_report(repo, "gitsummary", "# Report\n\nbody",
                             meta={"model": "Champ", "head": "abc123"})
    assert path.exists()
    assert path.name.startswith("gitsummary-")
    assert path.suffix == ".md"
    text = path.read_text()
    assert text.startswith("---\n")
    assert "kind: gitsummary" in text
    assert "model: Champ" in text
    assert "head: abc123" in text
    assert "# Report" in text
    # Lives under ~/.luxe/reports/<hash>/
    assert "reports" in str(path)


def test_save_report_two_calls_do_not_clash(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    p1 = store.save_report(repo, "gitreview", "a")
    p2 = store.save_report(repo, "gitreview", "b")
    assert p1 != p2  # uuid suffix avoids same-second collision


def test_aliased_group_resolves_aliases():
    import click

    from luxe.cli import AliasedGroup, apply_aliases

    group = AliasedGroup(name="root")

    @group.command(name="realcmd")
    def _realcmd():
        pass

    apply_aliases(group, {"al": "realcmd", "r": "realcmd"})
    assert group.get_command(None, "al").name == "realcmd"
    assert group.get_command(None, "r").name == "realcmd"
    assert group.get_command(None, "realcmd").name == "realcmd"
    assert group.get_command(None, "nope") is None


@pytest.mark.parametrize("alias,canonical", [
    ("gsum", "gitsummary"), ("git-summary", "gitsummary"),
    ("grev", "gitreview"), ("git-review", "gitreview"),
    ("gref", "gitrefactor"), ("git-refactor", "gitrefactor"),
    ("gitsummary", "gitsummary"),
])
def test_main_registers_gitkit_commands_and_aliases(alias, canonical):
    from luxe.cli import main
    resolved = main.get_command(None, alias)
    assert resolved is not None
    assert resolved.name == canonical
