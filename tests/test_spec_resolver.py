"""Tests for src/luxe/spec_resolver.py — chain assembly + glob matching (Lever 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe.sdd import SddFile, SddParseError
from luxe.spec_resolver import (
    ResolvedChain,
    resolve_chain,
    _compile_glob,
    _glob_matches,
    _normalize_rel,
)


# --- glob translation -----------------------------------------------------


class TestGlobMatching:
    @pytest.mark.parametrize(
        "glob,path,expected",
        [
            # `*` does not cross /
            ("*.py", "foo.py", True),
            ("*.py", "src/foo.py", False),
            # `**` crosses /
            ("**/*.py", "foo.py", True),
            ("**/*.py", "src/foo.py", True),
            ("**/*.py", "src/luxe/agents/loop.py", True),
            # leading dir + **
            ("tests/**", "tests/foo.py", True),
            ("tests/**", "tests/sub/bar.py", True),
            ("tests/**", "src/tests/foo.py", False),
            # `**/test_*.py` matches at any depth
            ("**/test_*.py", "test_foo.py", True),
            ("**/test_*.py", "tests/test_foo.py", True),
            ("**/test_*.py", "src/sub/test_x.py", True),
            ("**/test_*.py", "test.py", False),  # no underscore
            # Subtree
            ("src/luxe/**", "src/luxe/spec.py", True),
            ("src/luxe/**", "src/luxe/agents/loop.py", True),
            ("src/luxe/**", "src/swarm/foo.py", False),
            # `?` single char (no /)
            ("a?b", "axb", True),
            ("a?b", "a/b", False),
            ("a?b", "axxb", False),
            # Literal regex chars escaped
            ("foo.py", "foo.py", True),
            ("foo.py", "fooXpy", False),  # `.` is literal
            ("foo+bar", "foo+bar", True),
            ("foo+bar", "fooobar", False),
            # Bracket
            ("test_[abc].py", "test_a.py", True),
            ("test_[abc].py", "test_d.py", False),
            ("test_[!abc].py", "test_d.py", True),
            ("test_[!abc].py", "test_a.py", False),
        ],
    )
    def test_glob_matches(self, glob, path, expected):
        assert _glob_matches(glob, path) is expected, f"{glob!r} vs {path!r}"

    def test_compile_caches(self):
        p1 = _compile_glob("foo/*.py")
        p2 = _compile_glob("foo/*.py")
        assert p1 is p2

    def test_double_star_collapses_intermediate_separator(self):
        # `foo/**/bar` should match both `foo/bar` and `foo/x/bar`.
        assert _glob_matches("foo/**/bar", "foo/bar")
        assert _glob_matches("foo/**/bar", "foo/x/bar")
        assert _glob_matches("foo/**/bar", "foo/x/y/bar")


class TestNormalizeRel:
    def test_strips_leading_dot_slash(self):
        assert _normalize_rel("./foo.py") == "foo.py"
        assert _normalize_rel("././foo.py") == "foo.py"

    def test_strips_leading_slash(self):
        assert _normalize_rel("/foo.py") == "foo.py"

    def test_converts_backslashes(self):
        assert _normalize_rel("src\\luxe\\foo.py") == "src/luxe/foo.py"

    def test_idempotent(self):
        assert _normalize_rel("src/luxe/foo.py") == "src/luxe/foo.py"


# --- chain assembly -------------------------------------------------------


def _write_sdd(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestResolveChain:
    def test_no_sdd_in_chain(self, tmp_path):
        target = tmp_path / "src" / "foo.py"
        target.parent.mkdir(parents=True)
        target.write_text("print()")
        chain = resolve_chain(tmp_path, target)
        assert chain.files == []
        assert chain.target_rel == "src/foo.py"

    def test_picks_up_directly_named_sdd(self, tmp_path):
        # tmp_path/src/luxe/luxe.sdd exists; target = tmp_path/src/luxe/foo.py
        _write_sdd(
            tmp_path / "src" / "luxe" / "luxe.sdd",
            "# luxe\n## Forbids\n- tests/**\n",
        )
        target = tmp_path / "src" / "luxe" / "foo.py"
        target.write_text("x = 1")
        chain = resolve_chain(tmp_path, target)
        assert len(chain.files) == 1
        assert chain.files[0].title == "luxe"
        assert chain.files[0].forbids == ["tests/**"]

    def test_chain_is_ancestor_first(self, tmp_path):
        # Outer .sdd at src/luxe/luxe.sdd; inner at src/luxe/agents/agents.sdd.
        # Target deeper still. Expect outer first, inner last.
        _write_sdd(
            tmp_path / "src" / "luxe" / "luxe.sdd",
            "# luxe\n## Owns\n- src/luxe/**\n",
        )
        _write_sdd(
            tmp_path / "src" / "luxe" / "agents" / "agents.sdd",
            "# agents\n## Forbids\n- src/luxe/spec.py\n",
        )
        target = tmp_path / "src" / "luxe" / "agents" / "loop.py"
        target.write_text("x = 1")
        chain = resolve_chain(tmp_path, target)
        assert [sf.title for sf in chain.files] == ["luxe", "agents"]

    def test_skips_directories_without_matching_sdd(self, tmp_path):
        # tmp_path/src/.sdd exists at WRONG name (not src.sdd) — should be ignored.
        _write_sdd(
            tmp_path / "src" / "luxe.sdd",  # wrong: basename != dir name
            "# wrong\n## Must\n- ignored\n",
        )
        # Correct one at src/luxe/luxe.sdd
        _write_sdd(
            tmp_path / "src" / "luxe" / "luxe.sdd",
            "# luxe\n## Must\n- correct\n",
        )
        target = tmp_path / "src" / "luxe" / "foo.py"
        target.write_text("x")
        chain = resolve_chain(tmp_path, target)
        assert [sf.title for sf in chain.files] == ["luxe"]

    def test_target_is_directory(self, tmp_path):
        _write_sdd(
            tmp_path / "src" / "src.sdd",
            "# src\n",
        )
        target = tmp_path / "src"
        chain = resolve_chain(tmp_path, target)
        assert [sf.title for sf in chain.files] == ["src"]
        assert chain.target_rel == "src"

    def test_target_outside_repo_root_raises(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text("x")
        with pytest.raises(ValueError, match="not inside repo_root"):
            resolve_chain(repo, outside)

    def test_malformed_sdd_propagates(self, tmp_path):
        _write_sdd(
            tmp_path / "src" / "src.sdd",
            "## Must\n- a\n## Must\n- b\n",  # duplicate
        )
        target = tmp_path / "src" / "foo.py"
        target.write_text("x")
        with pytest.raises(SddParseError, match="duplicate"):
            resolve_chain(tmp_path, target)

    def test_repo_root_itself_with_matching_sdd(self, tmp_path):
        # Repo root named "myrepo" contains myrepo.sdd; target inside.
        repo = tmp_path / "myrepo"
        _write_sdd(repo / "myrepo.sdd", "# root\n## Owns\n- src/**\n")
        target = repo / "src" / "foo.py"
        target.parent.mkdir()
        target.write_text("x")
        chain = resolve_chain(repo, target)
        assert [sf.title for sf in chain.files] == ["root"]


# --- queries on the chain ------------------------------------------------


class TestChainQueries:
    @pytest.fixture
    def chain(self, tmp_path):
        _write_sdd(
            tmp_path / "src" / "luxe" / "luxe.sdd",
            "# luxe\n"
            "## Owns\n- src/luxe/**\n"
            "## Forbids\n- tests/**\n- **/secret_*.py\n",
        )
        _write_sdd(
            tmp_path / "src" / "luxe" / "agents" / "agents.sdd",
            "# agents\n"
            "## Owns\n- src/luxe/agents/**\n"
            "## Forbids\n- src/luxe/spec.py\n",
        )
        target = tmp_path / "src" / "luxe" / "agents" / "loop.py"
        target.write_text("x")
        return resolve_chain(tmp_path, target)

    def test_is_forbidden_finds_root_match(self, chain):
        hit, sf, glob = chain.is_forbidden("tests/foo.py")
        assert hit is True
        assert sf is not None
        assert sf.title == "luxe"
        assert glob == "tests/**"

    def test_is_forbidden_finds_leaf_match(self, chain):
        hit, sf, glob = chain.is_forbidden("src/luxe/spec.py")
        assert hit is True
        assert sf.title == "agents"
        assert glob == "src/luxe/spec.py"

    def test_is_forbidden_double_star_glob(self, chain):
        hit, sf, glob = chain.is_forbidden("src/luxe/secret_token.py")
        assert hit is True
        assert glob == "**/secret_*.py"

    def test_is_forbidden_returns_false_when_no_match(self, chain):
        hit, sf, glob = chain.is_forbidden("src/luxe/spec_validator.py")
        assert hit is False
        assert sf is None
        assert glob is None

    def test_is_owned_finds_root_match(self, chain):
        hit, sf, glob = chain.is_owned("src/luxe/spec.py")
        assert hit is True
        assert sf.title == "luxe"
        assert glob == "src/luxe/**"

    def test_is_owned_finds_leaf_match(self, chain):
        # Leaf `Owns: src/luxe/agents/**` is checked first only because
        # of root → leaf order; both root + leaf claim ownership of agents
        # files. We return the FIRST hit (root), which is fine — both
        # ownerships are valid.
        hit, sf, glob = chain.is_owned("src/luxe/agents/loop.py")
        assert hit is True
        assert sf.title == "luxe"  # root claimed it first

    def test_is_owned_returns_false_when_outside_chain(self, chain):
        hit, _, _ = chain.is_owned("docs/README.md")
        assert hit is False

    def test_all_forbids_lists_every_rule(self, chain):
        rules = chain.all_forbids()
        # Two from root (luxe.sdd) + one from leaf (agents.sdd) = 3
        assert len(rules) == 3
        sources = [sf.title for sf, _ in rules]
        globs = [g for _, g in rules]
        assert sources == ["luxe", "luxe", "agents"]
        assert "tests/**" in globs
        assert "**/secret_*.py" in globs
        assert "src/luxe/spec.py" in globs

    def test_path_normalization_in_queries(self, chain):
        # Caller passes various input shapes; chain normalizes.
        assert chain.is_forbidden("./tests/foo.py")[0] is True
        assert chain.is_forbidden("/tests/foo.py")[0] is True
        assert chain.is_forbidden("tests\\foo.py")[0] is True


class TestEmptyChainBehavior:
    def test_empty_chain_returns_negative_for_everything(self, tmp_path):
        target = tmp_path / "foo.py"
        target.write_text("x")
        chain = resolve_chain(tmp_path, target)
        assert chain.files == []
        assert chain.is_forbidden("anything")[0] is False
        assert chain.is_owned("anything")[0] is False
        assert chain.all_forbids() == []
