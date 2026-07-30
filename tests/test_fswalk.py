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

from luxe.fswalk import DEFAULT_SKIP_DIRS, iter_files, scan_source_files
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


# --- scan_source_files (bounded, shared index scan) --------------------------

_EXT = frozenset({".py", ".md"})


class TestScanSourceFiles:
    """One bounded scan feeds both the BM25 and symbol indexes. Chatting from
    `$HOME` used to cost 210s of indexing (measured 2026-07-30); the caps and
    the git fast path bring that to seconds, and every truncation is reported.
    """

    def test_finds_matching_files_and_ignores_others(self, tmp_path):
        _touch(tmp_path / "a.py")
        _touch(tmp_path / "pkg" / "b.md")
        _touch(tmp_path / "notes.txt")
        _touch(tmp_path / "image.png")

        scan = scan_source_files(tmp_path, extensions=_EXT, use_git=False)

        assert {p.name for p in scan.paths} == {"a.py", "b.md"}
        assert scan.used_git is False and scan.truncated == ""

    def test_uses_git_when_the_root_is_a_repo(self, tmp_path):
        import subprocess

        _touch(tmp_path / "tracked.py")
        _touch(tmp_path / "ignored.py")
        (tmp_path / ".gitignore").write_text("ignored.py\n")
        subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)

        scan = scan_source_files(tmp_path, extensions=_EXT)

        assert scan.used_git is True
        names = {p.name for p in scan.paths}
        assert "tracked.py" in names
        assert "ignored.py" not in names       # .gitignore respected for free

    def test_use_git_false_forces_the_walk(self, tmp_path):
        import subprocess

        _touch(tmp_path / "ignored.py")
        (tmp_path / ".gitignore").write_text("ignored.py\n")
        subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)

        scan = scan_source_files(tmp_path, extensions=_EXT, use_git=False)
        assert scan.used_git is False
        assert "ignored.py" in {p.name for p in scan.paths}

    def test_home_noise_dirs_are_pruned_only_at_the_top(self, tmp_path):
        _touch(tmp_path / "Library" / "junk.py")          # home fixture: skipped
        _touch(tmp_path / "proj" / "Library" / "real.py")  # a repo's own dir: kept

        scan = scan_source_files(tmp_path, extensions=_EXT, use_git=False)

        names = {p.name for p in scan.paths}
        assert "junk.py" not in names
        assert "real.py" in names

    def test_skip_dirs_apply_at_every_depth(self, tmp_path):
        _touch(tmp_path / "keep.py")
        _touch(tmp_path / "a" / "node_modules" / "dep.py")
        _touch(tmp_path / ".venv" / "lib.py")
        _touch(tmp_path / "b" / "site-packages" / "pkg.py")

        scan = scan_source_files(tmp_path, extensions=_EXT, use_git=False)
        assert {p.name for p in scan.paths} == {"keep.py"}

    def test_file_cap_truncates_and_says_so(self, tmp_path):
        for i in range(20):
            _touch(tmp_path / f"f{i:02d}.py")

        scan = scan_source_files(tmp_path, extensions=_EXT, max_files=5,
                                 use_git=False)

        assert scan.count == 5
        assert "5-file cap" in scan.truncated

    def test_byte_budget_truncates_and_says_so(self, tmp_path):
        for i in range(10):
            _touch(tmp_path / f"f{i}.py", "x" * 1024)

        scan = scan_source_files(tmp_path, extensions=_EXT,
                                 max_total_bytes=3 * 1024, use_git=False)

        assert scan.count <= 3
        assert "MB byte budget" in scan.truncated

    def test_oversized_files_are_skipped_and_counted(self, tmp_path):
        _touch(tmp_path / "small.py", "x")
        _touch(tmp_path / "huge.py", "x" * 5000)

        scan = scan_source_files(tmp_path, extensions=_EXT, max_file_bytes=1000,
                                 use_git=False)

        assert {p.name for p in scan.paths} == {"small.py"}
        assert scan.oversized == 1

    def test_scan_is_breadth_first_so_a_cap_keeps_shallow_files(self, tmp_path):
        """Depth-first would spend the whole budget inside one deep subtree."""
        _touch(tmp_path / "top.py")
        _touch(tmp_path / "aaa" / "mid.py")
        deep = tmp_path / "aaa" / "bbb" / "ccc"
        for i in range(10):
            _touch(deep / f"deep{i}.py")

        scan = scan_source_files(tmp_path, extensions=_EXT, max_files=2,
                                 use_git=False)

        names = [p.name for p in scan.paths]
        assert names[0] == "top.py"
        assert "mid.py" in names
        assert not any(n.startswith("deep") for n in names)

    def test_unreadable_directory_does_not_stop_the_scan(self, tmp_path, monkeypatch):
        _touch(tmp_path / "ok.py")
        doomed = tmp_path / "nas"
        doomed.mkdir()
        _touch(doomed / "unreachable.py")
        _fail_scandir_under(monkeypatch, doomed,
                            OSError(errno.ETIMEDOUT, "Operation timed out"))

        scan = scan_source_files(tmp_path, extensions=_EXT, use_git=False)

        assert {p.name for p in scan.paths} == {"ok.py"}

    def test_symlinked_directories_are_not_followed(self, tmp_path):
        _touch(tmp_path / "real" / "a.py")
        (tmp_path / "link").symlink_to(tmp_path / "real")

        scan = scan_source_files(tmp_path, extensions=_EXT, use_git=False)

        assert len(scan.paths) == 1      # not indexed twice via the symlink

    def test_progress_is_reported(self, tmp_path):
        for i in range(3):
            _touch(tmp_path / f"f{i}.py")
        seen: list[int] = []
        scan_source_files(tmp_path, extensions=_EXT, use_git=False,
                          on_progress=seen.append)
        assert seen and seen[-1] == 3    # final count always reported

    def test_records_wall_time(self, tmp_path):
        _touch(tmp_path / "a.py")
        scan = scan_source_files(tmp_path, extensions=_EXT, use_git=False)
        assert scan.seconds >= 0.0
