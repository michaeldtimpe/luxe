"""Tests for src/luxe/fswalk.py — the fault-tolerant repo walker.

Regression guard for the 2026-07-29 chat crash: `luxe chat` run from `$HOME`
walked into a Synology Drive placeholder tree, `os.scandir` raised
`OSError(ETIMEDOUT)`, and because `Path.rglob` only swallows `PermissionError`
the exception unwound through `find_all_sdd` and killed the Textual app.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from luxe.fswalk import DEFAULT_SKIP_DIRS, iter_files
from luxe.spec_resolver import find_all_sdd


def _touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _fail_scandir_under(monkeypatch, doomed: Path, exc: OSError) -> None:
    """Make `os.scandir` raise for `doomed` (and anything under it), simulating
    an unreachable network-backed directory."""
    real_scandir = os.scandir

    def fake_scandir(path=".", *args, **kwargs):
        p = Path(os.fspath(path))
        if p == doomed or doomed in p.parents:
            raise exc
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", fake_scandir)


class TestIterFiles:
    def test_yields_files_recursively(self, tmp_path):
        _touch(tmp_path / "a.py")
        _touch(tmp_path / "pkg" / "b.py")
        found = {p.relative_to(tmp_path).as_posix() for p in iter_files(tmp_path)}
        assert found == {"a.py", "pkg/b.py"}

    def test_name_filter_applies_to_basename(self, tmp_path):
        _touch(tmp_path / "a.py")
        _touch(tmp_path / "pkg" / "pkg.sdd")
        found = {p.name for p in iter_files(tmp_path,
                                            name_filter=lambda n: n.endswith(".sdd"))}
        assert found == {"pkg.sdd"}

    def test_prunes_default_skip_dirs(self, tmp_path):
        _touch(tmp_path / "keep.py")
        for skipped in (".git", "node_modules", ".venv", "__pycache__"):
            _touch(tmp_path / skipped / "buried.py")
        found = {p.name for p in iter_files(tmp_path)}
        assert found == {"keep.py"}
        assert {".git", "node_modules", ".venv", "__pycache__"} <= DEFAULT_SKIP_DIRS

    def test_survives_timed_out_directory(self, tmp_path, monkeypatch):
        # The crash shape: a network-backed dir whose scandir times out.
        _touch(tmp_path / "reachable" / "ok.py")
        doomed = tmp_path / "CloudStorage"
        doomed.mkdir()
        _fail_scandir_under(monkeypatch, doomed,
                            OSError(errno.ETIMEDOUT, "Operation timed out"))

        found = {p.name for p in iter_files(tmp_path)}

        assert found == {"ok.py"}  # unreadable subtree skipped, walk continued

    def test_survives_permission_error(self, tmp_path, monkeypatch):
        _touch(tmp_path / "reachable" / "ok.py")
        doomed = tmp_path / "private"
        doomed.mkdir()
        _fail_scandir_under(monkeypatch, doomed,
                            PermissionError(errno.EACCES, "Permission denied"))

        assert {p.name for p in iter_files(tmp_path)} == {"ok.py"}

    def test_unreadable_root_yields_nothing(self, tmp_path, monkeypatch):
        _fail_scandir_under(monkeypatch, tmp_path,
                            OSError(errno.ETIMEDOUT, "Operation timed out"))
        assert list(iter_files(tmp_path)) == []


class TestFindAllSddTolerance:
    def test_find_all_sdd_survives_timed_out_directory(self, tmp_path, monkeypatch):
        """The exact 2026-07-29 crash path: one unreachable subtree must not
        take down the turn — the reachable contracts still resolve."""
        _touch(tmp_path / "src" / "src.sdd", "# src\n## Must\n- a\n")
        doomed = tmp_path / "CloudStorage"
        doomed.mkdir()
        _fail_scandir_under(monkeypatch, doomed,
                            OSError(errno.ETIMEDOUT, "Operation timed out"))

        found = find_all_sdd(tmp_path)

        assert [sf.title for sf in found] == ["src"]

    def test_find_all_sdd_skips_vendored_contracts(self, tmp_path):
        # A `.sdd` inside a vendored/venv tree is not this repo's contract.
        _touch(tmp_path / "src" / "src.sdd", "# src\n## Must\n- a\n")
        _touch(tmp_path / ".venv" / "pkg" / "pkg.sdd", "# pkg\n## Must\n- b\n")

        assert [sf.title for sf in find_all_sdd(tmp_path)] == ["src"]


def test_pathlib_rglob_is_still_unsafe(tmp_path, monkeypatch):
    """Documents WHY this module exists: `Path.rglob` propagates every OSError
    that isn't PermissionError. If CPython ever fixes that, this test fails and
    the workaround can be revisited."""
    doomed = tmp_path / "CloudStorage"
    doomed.mkdir()
    _fail_scandir_under(monkeypatch, doomed,
                        OSError(errno.ETIMEDOUT, "Operation timed out"))

    with pytest.raises(OSError):
        list(tmp_path.rglob("*.sdd"))
