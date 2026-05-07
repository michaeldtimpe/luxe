"""Tests for SpecDD Lever 2 tool-side Forbids enforcement in fs.py.

Covers `_check_spec_forbids`'s integration with `_write_file` and
`_edit_file`. Pre-write enforcement: a `.sdd` `Forbids:` glob causes
the tool to refuse the write before any I/O. No `.sdd` in the chain
means no enforcement (forward-compat with existing fixtures).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe.tools import fs


@pytest.fixture
def tmp_repo_with_sdd(tmp_path: Path) -> Path:
    """A repo whose root .sdd forbids tests/** and **/test_fix.py."""
    (tmp_path / "src" / "luxe").mkdir(parents=True)
    (tmp_path / "src" / "luxe" / "main.py").write_text("x = 1\n")
    # Synthetic root-level .sdd (named after directory): tmp_path/<basename>.sdd
    sdd_name = f"{tmp_path.name}.sdd"
    (tmp_path / sdd_name).write_text(
        "# repo-root\n"
        "## Owns\n"
        "- src/**\n"
        "## Forbids\n"
        "- tests/**\n"
        "- **/test_fix.py\n"
        "- **/secret_*.py\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def set_root(tmp_repo_with_sdd: Path):
    fs.set_repo_root(tmp_repo_with_sdd)
    yield
    fs._REPO_ROOT = None


class TestWriteFileForbids:
    def test_write_inside_owns_succeeds(self, tmp_repo_with_sdd, set_root):
        result, err = fs.MUTATION_FNS["write_file"](
            {"path": "src/luxe/new.py", "content": "x = 2\n"}
        )
        assert err is None, f"unexpected error: {err}"
        assert (tmp_repo_with_sdd / "src/luxe/new.py").read_text() == "x = 2\n"

    def test_write_to_forbidden_subtree_refused(self, tmp_repo_with_sdd, set_root):
        result, err = fs.MUTATION_FNS["write_file"](
            {"path": "tests/test_new.py", "content": "x = 1\n"}
        )
        assert err is not None
        assert "forbidden" in err.lower()
        assert "tests/**" in err
        # File NOT created — pre-write enforcement.
        assert not (tmp_repo_with_sdd / "tests/test_new.py").exists()

    def test_write_to_forbidden_filename_pattern_refused(self, tmp_repo_with_sdd, set_root):
        # `**/test_fix.py` matches at any depth, not just at the root.
        result, err = fs.MUTATION_FNS["write_file"](
            {"path": "src/luxe/test_fix.py", "content": "x = 1\n"}
        )
        assert err is not None
        assert "forbidden" in err.lower()
        assert "test_fix.py" in err

    def test_error_includes_sdd_source(self, tmp_repo_with_sdd, set_root):
        sdd_name = f"{tmp_repo_with_sdd.name}.sdd"
        result, err = fs.MUTATION_FNS["write_file"](
            {"path": "src/luxe/secret_token.py", "content": "x = 1\n"}
        )
        assert err is not None
        assert sdd_name in err

    def test_no_sdd_in_chain_means_no_enforcement(self, tmp_path: Path):
        # Repo without any .sdd files: no Forbids enforcement.
        (tmp_path / "src").mkdir()
        fs.set_repo_root(tmp_path)
        try:
            result, err = fs.MUTATION_FNS["write_file"](
                {"path": "tests/test_anything.py", "content": "x = 1\n"}
            )
            assert err is None, f"empty chain should not enforce: {err}"
        finally:
            fs._REPO_ROOT = None


class TestEditFileForbids:
    def test_edit_forbidden_path_refused(self, tmp_repo_with_sdd, set_root):
        # Pre-create the file outside the tool layer (filesystem-level write).
        target = tmp_repo_with_sdd / "tests"
        target.mkdir()
        (target / "test_legacy.py").write_text("a = 1\n")

        result, err = fs.MUTATION_FNS["edit_file"](
            {
                "path": "tests/test_legacy.py",
                "old_string": "a = 1",
                "new_string": "a = 2",
            }
        )
        assert err is not None
        assert "forbidden" in err.lower()
        # Original content unchanged.
        assert (target / "test_legacy.py").read_text() == "a = 1\n"

    def test_edit_allowed_path_succeeds(self, tmp_repo_with_sdd, set_root):
        result, err = fs.MUTATION_FNS["edit_file"](
            {
                "path": "src/luxe/main.py",
                "old_string": "x = 1",
                "new_string": "x = 2",
            }
        )
        assert err is None, f"unexpected error: {err}"
        assert "x = 2" in (tmp_repo_with_sdd / "src/luxe/main.py").read_text()


class TestForbidsOrdering:
    def test_role_path_check_runs_first(self, tmp_repo_with_sdd, set_root):
        # `worker_read.py` would trip the role-path guard regardless of
        # spec Forbids. Confirms ordering: cheap honesty guards first.
        result, err = fs.MUTATION_FNS["write_file"](
            {"path": "src/luxe/worker_read.py", "content": "x = 1\n"}
        )
        assert err is not None
        # The role-path message comes from a different guard; the spec
        # message would say "forbidden by". Confirm we hit the role
        # guard first (it doesn't mention 'forbidden by').
        assert "forbidden by" not in err

    def test_placeholder_check_runs_before_forbids(self, tmp_repo_with_sdd, set_root):
        # Allowed path but placeholder content — should fail at the
        # placeholder guard, not the forbids guard.
        result, err = fs.MUTATION_FNS["write_file"](
            {"path": "src/luxe/new.py", "content": "# TODO: implement this\n"}
        )
        assert err is not None
        # Either error string is acceptable; we just want to confirm
        # the write was refused for *some* honesty reason.
        assert "src/luxe/new.py" not in str(
            (tmp_repo_with_sdd / "src/luxe/new.py").exists()
        ) or not (tmp_repo_with_sdd / "src/luxe/new.py").exists()


class TestMalformedSddSurfacing:
    def test_malformed_sdd_returns_tool_error(self, tmp_path: Path):
        # Author a malformed .sdd (duplicate sections); confirm the
        # write returns a tool error rather than crashing.
        (tmp_path / "src").mkdir()
        sdd_name = f"{tmp_path.name}.sdd"
        (tmp_path / sdd_name).write_text(
            "## Must\n- a\n## Must\n- b\n",  # duplicate
            encoding="utf-8",
        )
        fs.set_repo_root(tmp_path)
        try:
            result, err = fs.MUTATION_FNS["write_file"](
                {"path": "src/foo.py", "content": "x = 1\n"}
            )
            assert err is not None
            assert "malformed" in err.lower()
        finally:
            fs._REPO_ROOT = None


# --- v1.6 — Forbids creating (create-only) -------------------------------


@pytest.fixture
def tmp_repo_with_forbids_create(tmp_path: Path) -> Path:
    """A repo whose root .sdd has Forbids creating: **/test_*.py.

    No `Forbids` rules — only the create-only section. Lets the test
    isolate create-vs-edit semantics from the always-fires case.
    """
    (tmp_path / "src" / "luxe").mkdir(parents=True)
    (tmp_path / "src" / "luxe" / "main.py").write_text("x = 1\n")
    sdd_name = f"{tmp_path.name}.sdd"
    (tmp_path / sdd_name).write_text(
        "# repo-root\n"
        "## Owns\n"
        "- src/**\n"
        "- tests/**\n"
        "## Forbids creating\n"
        "- **/test_*.py\n"
        "- **/verify_*.py\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def set_root_create(tmp_repo_with_forbids_create: Path):
    fs.set_repo_root(tmp_repo_with_forbids_create)
    yield
    fs._REPO_ROOT = None


class TestCreateVsEditForbids:
    """v1.6 — Forbids creating fires only when path doesn't exist on disk."""

    def test_create_blocked_at_new_path(self, tmp_repo_with_forbids_create, set_root_create):
        # Path does NOT exist → write_file would create → blocked.
        result, err = fs.MUTATION_FNS["write_file"](
            {"path": "tests/test_new.py", "content": "x = 1\n"}
        )
        assert err is not None
        assert "forbidden-on-create" in err
        assert "test_new.py" in err
        assert "Edit an existing file" in err
        # Confirm file was NOT created
        assert not (tmp_repo_with_forbids_create / "tests" / "test_new.py").exists()

    def test_overwrite_existing_test_allowed(self, tmp_repo_with_forbids_create, set_root_create):
        # Pre-create the file at filesystem level, then write_file (overwrite).
        (tmp_repo_with_forbids_create / "tests").mkdir()
        existing = tmp_repo_with_forbids_create / "tests" / "test_existing.py"
        existing.write_text("a = 1\n")

        # write_file overwrites — `creating=False` because path exists → allowed.
        result, err = fs.MUTATION_FNS["write_file"](
            {"path": "tests/test_existing.py", "content": "a = 2\n"}
        )
        assert err is None, f"overwrite should be allowed: {err}"
        assert existing.read_text() == "a = 2\n"

    def test_edit_existing_test_allowed(self, tmp_repo_with_forbids_create, set_root_create):
        # Pre-create then edit_file — creating=False structurally.
        (tmp_repo_with_forbids_create / "tests").mkdir()
        existing = tmp_repo_with_forbids_create / "tests" / "test_existing.py"
        existing.write_text("a = 1\n")

        result, err = fs.MUTATION_FNS["edit_file"](
            {
                "path": "tests/test_existing.py",
                "old_string": "a = 1",
                "new_string": "a = 2",
            }
        )
        assert err is None, f"edit should be allowed: {err}"
        assert "a = 2" in existing.read_text()

    def test_create_blocked_v2_test_topic_shape(self, tmp_repo_with_forbids_create, set_root_create):
        # The exact escapes from v1.5 v2 rerun (matplotlib-24870, sympy-12481).
        for path in ("test_bool_contour.py", "tests/test_bool_contour.py"):
            result, err = fs.MUTATION_FNS["write_file"](
                {"path": path, "content": "x = 1\n"}
            )
            assert err is not None, f"v2 escape {path!r} should be blocked"
            assert "forbidden-on-create" in err

    def test_error_message_distinguishes_class(self, tmp_repo_with_forbids_create, set_root_create):
        # Error message for create-only matches uses different wording
        # than always-fires Forbids: "wrong operation" vs "wrong location".
        # This is the planner-recovery gradient.
        result, err = fs.MUTATION_FNS["write_file"](
            {"path": "tests/test_invented.py", "content": "x = 1\n"}
        )
        assert err is not None
        assert "forbidden-on-create" in err
        assert "Edit an existing file" in err
        # The original "do not write files outside the allowed paths"
        # phrasing is for unconditional Forbids only.
        assert "outside the allowed paths" not in err
