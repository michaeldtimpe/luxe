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

    # --- Honesty guards (write-time defences against Phase 2 failure modes) ---

    def test_write_rejects_placeholder_text(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "stub.js",
            "content": "<paste the modified content here>",
        })
        assert err is not None
        assert "placeholder" in err.lower()
        assert not (tmp_repo / "stub.js").exists()

    def test_write_rejects_your_code_here(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "handler.js",
            "content": "function reset() {\n  // Your reset code here\n}",
        })
        assert err is not None
        assert "placeholder" in err.lower()

    def test_write_rejects_role_named_path(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "src/worker_read.js",
            "content": "console.log('ok');",
        })
        assert err is not None
        assert "role" in err.lower() and "worker_read" in err

    def test_write_rejects_role_named_in_subdir(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "src/input/worker_analyze/reset.py",
            "content": "def reset(): pass",
        })
        assert err is not None
        assert "worker_analyze" in err

    def test_write_rejects_mass_deletion(self, tmp_repo: Path):
        # Create a 60-line file then try to overwrite with a 2-line stub.
        (tmp_repo / "big.py").write_text("\n".join(f"line {i}" for i in range(60)))
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "big.py",
            "content": "def reset(): pass\n",
        })
        assert err is not None
        assert "mass-deletion" in err.lower() or "stub" in err.lower()
        # Original file untouched.
        assert (tmp_repo / "big.py").read_text().count("\n") >= 50

    def test_write_allows_legit_short_file(self, tmp_repo: Path):
        # A genuinely small new file should not trip the mass-deletion gate.
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "small_helper.py",
            "content": "X = 1\n",
        })
        assert err is None

    def test_write_allows_full_rewrite(self, tmp_repo: Path):
        # A full rewrite (large → large) should pass.
        (tmp_repo / "rewrite.py").write_text("\n".join(f"old{i}" for i in range(60)))
        new = "\n".join(f"new{i}" for i in range(60))
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "rewrite.py",
            "content": new,
        })
        assert err is None

    def test_edit_rejects_placeholder_in_replacement(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["edit_file"]({
            "path": "src/main.py",
            "old_string": "Hello",
            "new_string": "// TODO: implement greeting",
        })
        assert err is not None
        assert "placeholder" in err.lower()

    def test_edit_rejects_role_named_path(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["edit_file"]({
            "path": "drafter.py",
            "old_string": "x", "new_string": "y",
        })
        assert err is not None
        assert "drafter" in err

    def test_edit_rejects_mass_deletion(self, tmp_repo: Path):
        big = "\n".join(f"line {i}" for i in range(60))
        (tmp_repo / "shrink.py").write_text(big)
        result, err = fs.MUTATION_FNS["edit_file"]({
            "path": "shrink.py",
            "old_string": big,
            "new_string": "x = 1\n",
        })
        assert err is not None
        assert "mass-deletion" in err.lower() or "stub" in err.lower()

    # --- Evasion regressions: actual fail patterns from the Phase 2 re-test ---

    def test_write_rejects_role_name_with_suffix(self, tmp_repo: Path):
        # Model wrote `worker_read_r.py` to evade exact-stem matching.
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "src/worker_read_r.py",
            "content": "x = 1\n",
        })
        assert err is not None
        assert "worker_read" in err

    def test_write_rejects_role_name_with_prefix(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "src/my_drafter.py",
            "content": "x = 1\n",
        })
        assert err is not None
        assert "drafter" in err

    def test_write_allows_encoder_decoder(self, tmp_repo: Path):
        # "coder" intentionally excluded from single-token check so legit
        # names like encoder.py / decoder.py / transcoder.py pass.
        for name in ("encoder.py", "decoder.py", "transcoder.py"):
            result, err = fs.MUTATION_FNS["write_file"]({
                "path": f"src/{name}", "content": "x = 1\n",
            })
            assert err is None, f"{name}: unexpectedly rejected: {err}"

    def test_write_rejects_multi_word_placeholder(self, tmp_repo: Path):
        # Model wrote `# Your real listener code here` to evade single-word.
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "handler.js",
            "content": "function reset() {\n  // Your real listener code here\n}",
        })
        assert err is not None
        assert "placeholder" in err.lower()

    def test_write_rejects_attach_listener_here(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "h.js",
            "content": "// Attach the keydown listener here\n",
        })
        assert err is not None
        assert "placeholder" in err.lower()

    def test_write_rejects_real_logic_belongs_here(self, tmp_repo: Path):
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "h.py",
            "content": "# Real handler logic belongs here\n",
        })
        assert err is not None
        assert "placeholder" in err.lower()

    def test_write_allows_legitimate_todo_comment(self, tmp_repo: Path):
        # Real-world TODO comments shouldn't trip the gate. The gate fires
        # only on TODO followed by a trigger verb, not bare TODOs.
        result, err = fs.MUTATION_FNS["write_file"]({
            "path": "feature.py",
            "content": "# TODO: deprecation tracker\nx = 1\n",
        })
        assert err is None


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
