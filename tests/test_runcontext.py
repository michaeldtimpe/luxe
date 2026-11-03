"""luxe/runcontext.py: the opt-in context pack.

Pins the exact-"1" gate, test-file matching, the pack's cap, that the pack
carries pointers and never file contents, and that git trouble degrades to a
shorter block instead of raising.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe import runcontext as rc
from luxe import search as search_mod
from luxe.agents import prompts
from tests._git import git


# --- gates -------------------------------------------------------------------

@pytest.mark.parametrize("value,on", [("1", True), ("0", False), ("true", False),
                                      ("", False), (None, False)])
def test_gate_is_exact_string_one(value, on):
    env = {} if value is None else {rc.PACK_ENV: value}
    assert rc.pack_enabled(env) is on


# --- test-file matching ------------------------------------------------------

@pytest.mark.parametrize("path,is_test", [
    ("tests/test_calc.py", True), ("src/calc_test.go", True),
    ("src/calc.test.ts", True), ("web/__tests__/calc.js", True),
    ("spec/calc.spec.rb", True), ("src/calc.py", False),
    ("src/contest.py", False), ("docs/testing.md", False),
])
def test_is_test_path(path, is_test):
    assert rc.is_test_path(path) is is_test


def test_related_tests_match_by_core_stem_and_skip_index():
    tracked = ["tests/test_calc.py", "src/calc.test.ts", "tests/test_other.py",
               "src/__tests__/index.js", "src/calc.py"]
    got = rc.related_tests(["src/calc.py", "src/index.js"], tracked)
    assert got == ["src/calc.test.ts", "tests/test_calc.py"]


# --- context pack ------------------------------------------------------------

def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b  # SECRET_BODY_MARKER\n")
    (repo / "src" / "shapes.py").write_text("class Square:\n    side = 1\n")
    (repo / "tests" / "test_calc.py").write_text("from src.calc import add\n")
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base: calculator")
    (repo / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b  # SECRET_BODY_MARKER\n\n"
        "def sub(a, b):\n    return a - b\n")
    git(repo, "commit", "-qam", "calc: add subtraction")
    return repo


def test_pack_points_at_files_commits_tests_and_recent(tmp_path):
    repo = _repo(tmp_path)
    idx = search_mod.build_bm25_index(repo)
    out = rc.context_pack(repo, "add a multiply function next to add and sub", idx)
    assert out.startswith(prompts.CONTEXT_PACK_HEADER)
    assert "- src/calc.py" in out
    assert "calc: add subtraction" in out
    assert prompts.CONTEXT_PACK_TESTS in out and "- tests/test_calc.py" in out
    assert prompts.CONTEXT_PACK_RECENT in out
    assert "SECRET_BODY_MARKER" not in out, "the pack must never quote file contents"


def test_pack_is_capped(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    for i in range(60):
        (repo / "src" / f"calc_mod_{i:02d}_with_a_long_name.py").write_text(
            "def add(a, b):\n    return a + b\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "many calc modules")
    monkeypatch.setattr(rc, "PACK_TOP_FILES", 40)
    monkeypatch.setattr(rc, "PACK_RECENT_FILES", 60)
    idx = search_mod.build_bm25_index(repo)
    out = rc.context_pack(repo, "add calc", idx)
    assert len(out) <= rc.PACK_MAX_CHARS
    assert out.rstrip().endswith(prompts.CONTEXT_PACK_TRUNCATED)


def test_pack_subjects_are_truncated(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    git(repo, "commit", "-qam", "calc: " + "y" * 300)
    out = rc.context_pack(repo, "add", search_mod.build_bm25_index(repo))
    assert "y" * 100 not in out


def test_pack_without_git_degrades_to_hits_only(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (plain / "shapes.py").write_text("class Square:\n    side = 1\n")
    (plain / "colors.py").write_text("RED = 1\n")  # BM25 idf is 0 at n=2
    out = rc.context_pack(plain, "add", search_mod.build_bm25_index(plain))
    assert "- calc.py" in out
    assert prompts.CONTEXT_PACK_RECENT not in out


def test_pack_with_nothing_to_point_at_is_empty(tmp_path):
    empty = tmp_path / "e"
    empty.mkdir()
    assert rc.context_pack(empty, "anything", None) == ""

