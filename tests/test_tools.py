"""Tests for tool implementations."""

from pathlib import Path

import pytest

from luxe.tools import fs
from luxe.tools.base import ToolCache, dispatch_tool, validate_args


@pytest.fixture(autouse=True)
def set_root(tmp_repo: Path):
    fs.set_repo_root(tmp_repo)
    yield
    fs._REPO_ROOT = None


class TestFsTools:
    def test_read_file(self, tmp_repo: Path):
        result, err = fs.READ_ONLY_FNS["read_file"]({"path": "src/main.py"})
        assert err is None
        assert "greet" in result
        assert "1\t" in result  # line numbers

    def test_read_file_not_found(self):
        result, err = fs.READ_ONLY_FNS["read_file"]({"path": "nonexistent.py"})
        assert err is not None
        assert "not found" in err.lower()

    def test_list_dir(self, tmp_repo: Path):
        result, err = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        assert err is None
        assert "src/" in result
        assert "README.md" in result

    def test_glob(self, tmp_repo: Path):
        result, err = fs.READ_ONLY_FNS["glob"]({"pattern": "**/*.py"})
        assert err is None
        assert "main.py" in result

    def test_grep(self, tmp_repo: Path):
        result, err = fs.READ_ONLY_FNS["grep"]({"pattern": "def greet"})
        assert err is None
        assert "greet" in result

    def test_write_file(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"](
            {"path": "new_file.py", "content": "print('hello')"}
        )
        assert err is None
        assert (tmp_repo / "new_file.py").read_text() == "print('hello')"

    def test_edit_file(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["edit_file"]({
            "path": "src/main.py",
            "old_string": "Hello",
            "new_string": "Hi",
        })
        assert err is None
        assert "Hi" in (tmp_repo / "src" / "main.py").read_text()

    def test_path_escape(self, tmp_repo: Path):
        with pytest.raises(PermissionError):
            fs._safe("../../etc/passwd")


class TestToolCache:
    def test_cache_hit(self):
        cache = ToolCache()
        fn = lambda args: ("result", None)
        r1, e1, cached1 = cache.get_or_run("test", {"a": 1}, fn)
        r2, e2, cached2 = cache.get_or_run("test", {"a": 1}, fn)
        assert not cached1
        assert cached2
        assert cache.hits == 1
        assert cache.misses == 1

    def test_cache_miss_different_args(self):
        cache = ToolCache()
        fn = lambda args: (str(args), None)
        cache.get_or_run("test", {"a": 1}, fn)
        _, _, cached = cache.get_or_run("test", {"a": 2}, fn)
        assert not cached


class TestValidation:
    def test_valid_args(self):
        defn = fs.read_only_defs()[0]  # read_file
        err = validate_args(defn, {"path": "test.py"})
        assert err is None

    def test_missing_required(self):
        defn = fs.read_only_defs()[0]
        err = validate_args(defn, {})
        assert err is not None
        assert "required" in err.lower()
