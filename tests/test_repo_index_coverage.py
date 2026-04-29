"""Tests for src/luxe/repo_index.py — symbol_index_coverage transparency."""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe.repo_index import RepoSummary, build_repo_summary


@pytest.fixture
def mixed_repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x = 1\n" * 50)
    (tmp_path / "src" / "lib.py").write_text("y = 2\n" * 30)
    (tmp_path / "Main.java").write_text("class Main {}\n" * 100)
    (tmp_path / "README.md").write_text("# repo\n")
    return tmp_path


def test_summary_counts_files_and_loc(mixed_repo: Path):
    summary = build_repo_summary(mixed_repo)
    assert summary.file_count == 4
    assert summary.total_loc > 0


def test_summary_languages_breakdown(mixed_repo: Path):
    summary = build_repo_summary(mixed_repo)
    assert summary.languages.get("python", 0) == 2
    assert summary.languages.get("java", 0) == 1
    assert summary.languages.get("markdown", 0) == 1


def test_summary_renders_with_coverage(mixed_repo: Path):
    coverage = {"python": 2}  # AST covers Python only
    summary = build_repo_summary(mixed_repo, symbol_coverage=coverage)
    rendered = summary.render()
    assert "AST symbol index coverage" in rendered
    assert "python (2)" in rendered
    # Java + markdown are uncovered (Python is the only AST-covered language).
    assert "Falls back to `bm25_search` for:" in rendered
    assert "java" in rendered
    assert "markdown" in rendered


def test_summary_renders_without_coverage(mixed_repo: Path):
    """When no coverage is provided, no coverage section is emitted."""
    summary = build_repo_summary(mixed_repo, symbol_coverage={})
    rendered = summary.render()
    assert "AST symbol index coverage" not in rendered


def test_summary_excludes_node_modules(tmp_path: Path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("export = 1\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x = 1\n")
    summary = build_repo_summary(tmp_path)
    assert summary.file_count == 1
    assert summary.languages == {"python": 1}


def test_summary_largest_files_sorted(tmp_path: Path):
    (tmp_path / "small.py").write_text("a = 1\n")
    (tmp_path / "large.py").write_text("b = 2\n" * 1000)
    summary = build_repo_summary(tmp_path)
    largest = [f.rel_path for f in summary.largest_files]
    assert largest[0] == "large.py"
