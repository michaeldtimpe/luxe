"""Path canonicalization for the fs tools (2026-09-26).

Every fs tool resolves a model-supplied path through ONE function,
`fs._resolve_rel`, and every guard (role-path, SpecDD Forbids) runs on the
canonical repo-relative posix path it returns — never on the raw string.
Before this, `_safe` scoped with a string `startswith` (a sibling directory
`../repo2` of root `.../repo` passed) and the guards read the RAW path while
the write went to the RESOLVED one, so `src/../tests/x.py` or an absolute
`<root>/tests/x.py` walked straight past `Forbids: tests/**`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe.tools import fs


@pytest.fixture
def repo(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    (root / "repo.sdd").write_text(
        "# repo\n## Owns\n- src/**\n## Forbids\n- tests/**\n", encoding="utf-8")
    sibling = tmp_path / "repo2"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("outside\n")
    fs.set_repo_root(root)
    yield root
    fs._REPO_ROOT = None


# --- 1. sibling-prefix escape ------------------------------------------------

class TestSiblingPrefixEscape:
    def test_resolver_rejects_sibling_with_shared_prefix(self, repo):
        with pytest.raises(PermissionError):
            fs._resolve_rel("../repo2/secret.txt")

    def test_read_file_cannot_read_sibling(self, repo):
        with pytest.raises(PermissionError):
            fs._read_file({"path": "../repo2/secret.txt"})

    def test_list_dir_cannot_list_sibling(self, repo):
        with pytest.raises(PermissionError):
            fs._list_dir({"path": "../repo2"})

    def test_write_file_cannot_write_sibling(self, repo):
        _, err = fs._write_file({"path": "../repo2/new.txt", "content": "x\n"})
        assert err and "escapes repo root" in err
        assert not (repo.parent / "repo2" / "new.txt").exists()


# --- 2. guards run on the canonical path ------------------------------------

class TestGuardsSeeCanonicalPath:
    def test_resolver_returns_canonical_posix_rel(self, repo):
        abs_path, rel = fs._resolve_rel("src/../src/./a.py")
        assert abs_path == repo / "src" / "a.py"
        assert rel == "src/a.py"
        assert fs._resolve_rel(".")[1] == "."
        assert fs._resolve_rel(str(repo / "src" / "a.py"))[1] == "src/a.py"

    def test_dotdot_cannot_bypass_forbids_on_write(self, repo):
        _, err = fs._write_file({"path": "src/../tests/b.py", "content": "x = 1\n"})
        assert err and "forbidden" in err
        assert not (repo / "tests" / "b.py").exists()

    def test_absolute_path_cannot_bypass_forbids_on_write(self, repo):
        _, err = fs._write_file(
            {"path": str(repo / "tests" / "d.py"), "content": "x = 1\n"})
        assert err and "forbidden" in err
        assert not (repo / "tests" / "d.py").exists()

    def test_dotdot_cannot_bypass_forbids_on_edit(self, repo):
        (repo / "tests").mkdir()
        (repo / "tests" / "t.py").write_text("a = 1\n")
        _, err = fs._edit_file({"path": "src/../tests/t.py",
                                "old_string": "a = 1", "new_string": "a = 2"})
        assert err and "forbidden" in err
        assert (repo / "tests" / "t.py").read_text() == "a = 1\n"

    def test_absolute_path_under_role_named_dir_is_not_a_role_leak(self, tmp_path):
        # The repo itself lives under a directory whose name is a role label.
        root = tmp_path / "validator" / "proj"
        (root / "src").mkdir(parents=True)
        fs.set_repo_root(root)
        try:
            out, err = fs._write_file(
                {"path": str(root / "src" / "ok.py"), "content": "y = 2\n"})
            assert err is None, err
            assert (root / "src" / "ok.py").read_text() == "y = 2\n"
        finally:
            fs._REPO_ROOT = None

    def test_role_label_inside_repo_still_refused(self, repo):
        _, err = fs._write_file({"path": "src/./drafter.py", "content": "x\n"})
        assert err and "role label" in err

    def test_ordinary_relative_paths_keep_their_messages(self, repo):
        out, err = fs._write_file({"path": "src/new.py", "content": "z = 3\n"})
        assert (out, err) == ("Wrote 6 bytes to src/new.py", None)
        out, err = fs._edit_file({"path": "src/new.py", "old_string": "z = 3",
                                  "new_string": "z = 4"})
        assert (out, err) == ("Edited src/new.py (1 replacement)", None)
        _, err = fs._write_file({"path": "tests/x.py", "content": "x = 1\n"})
        assert err.startswith("refusing to write 'tests/x.py': forbidden by repo.sdd")


# --- 8. edit_file with an empty old_string -----------------------------------

class TestEditEmptyOldString:
    def test_empty_old_string_refused_even_with_replace_all(self, repo):
        _, err = fs._edit_file({"path": "src/a.py", "old_string": "",
                                "new_string": "Z", "replace_all": True})
        assert err and "old_string" in err and "empty" in err
        assert (repo / "src" / "a.py").read_text() == "x = 1\n"


# --- 9. read_file refusals advise a grep call that exists ---------------------

class TestReadRefusalAdvisesRealGrepParam:
    def test_too_large_advice_uses_glob_not_path(self, repo):
        big = repo / "src" / "big.txt"
        big.write_text("line\n" * 70000)   # > 256 KB
        _, err = fs._read_file({"path": "src/big.txt"})
        assert 'grep(pattern="...", glob="src/big.txt")' in err
        assert "path=" not in err.split("grep(", 1)[1]

    def test_single_huge_line_advice_uses_glob(self, repo):
        (repo / "src" / "min.js").write_text("a" * (300 * 1024))
        _, err = fs._read_file({"path": "src/min.js", "limit": 5})
        assert 'grep(pattern="...", glob="src/min.js")' in err

    def test_the_advised_grep_call_actually_scopes_to_the_file(self, repo):
        (repo / "src" / "b.py").write_text("needle = 1\n")
        (repo / "src" / "c.py").write_text("needle = 2\n")
        out, err = fs._grep({"pattern": "needle", "glob": "src/b.py"})
        assert err is None
        assert "src/b.py" in out and "src/c.py" not in out

    def test_python_fallback_honours_a_path_glob(self, repo):
        (repo / "src" / "b.py").write_text("needle = 1\n")
        (repo / "src" / "c.py").write_text("needle = 2\n")
        out, err = fs._grep_python("needle", "src/b.py")
        assert err is None
        assert "src/b.py" in out and "src/c.py" not in out


# --- 10. glob: pruning and clean refusals ------------------------------------

class TestGlobPruneAndRefusals:
    def test_vendor_dirs_do_not_fill_the_result(self, repo):
        venv = repo / ".venv" / "lib"
        venv.mkdir(parents=True)
        for i in range(200):
            (venv / f"m{i:03}.py").write_text("")
        (repo / "node_modules" / "x").mkdir(parents=True)
        (repo / "node_modules" / "x" / "i.py").write_text("")
        out, err = fs._glob({"pattern": "**/*.py"})
        assert err is None
        assert out == "src/a.py"

    def test_explicitly_named_vendor_dir_still_searchable(self, repo):
        (repo / ".venv").mkdir()
        (repo / ".venv" / "pyvenv.cfg").write_text("")
        out, err = fs._glob({"pattern": ".venv/*.cfg"})
        assert err is None and out == ".venv/pyvenv.cfg"

    def test_dotdot_pattern_refused(self, repo):
        out, err = fs._glob({"pattern": "../*"})
        assert out == "" and err and ".." in err

    def test_absolute_pattern_refused_cleanly(self, repo):
        out, err = fs._glob({"pattern": "/etc/*"})
        assert out == "" and err and "relative" in err

    def test_empty_pattern_refused_cleanly(self, repo):
        out, err = fs._glob({"pattern": ""})
        assert out == "" and err and "pattern" in err
