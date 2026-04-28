"""Tests for src/luxe/locks.py — per-repo concurrency locks."""

from __future__ import annotations

import os
import subprocess
import time
from multiprocessing import Process
from pathlib import Path

import pytest

from luxe.locks import (
    LockHeld,
    acquire_repo_lock,
    lock_path_for,
    read_lock_info,
)


@pytest.fixture(autouse=True)
def _isolate_lock_dir(tmp_path, monkeypatch):
    """Force lock_dir() to a per-test tmp dir so we don't collide with real runs."""
    monkeypatch.setattr("luxe.locks.lock_dir", lambda: tmp_path / "locks")


def test_acquire_and_release(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with acquire_repo_lock(repo, run_id="abc123"):
        info = read_lock_info(repo)
        assert info is not None
        assert info.run_id == "abc123"
        assert info.pid == os.getpid()
    # After release: lockfile still exists but is no longer held — re-acquire works.
    with acquire_repo_lock(repo, run_id="def456"):
        info = read_lock_info(repo)
        assert info.run_id == "def456"


def test_double_acquire_fast_fails(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with acquire_repo_lock(repo, run_id="first"):
        with pytest.raises(LockHeld) as exc:
            with acquire_repo_lock(repo, run_id="second"):
                pytest.fail("should not enter — lock is held")
        assert exc.value.info.run_id == "first"
        assert exc.value.info.pid == os.getpid()


def test_lock_held_message_includes_pid_and_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with acquire_repo_lock(repo, run_id="held"):
        with pytest.raises(LockHeld) as exc:
            with acquire_repo_lock(repo, run_id="other"):
                pass
        msg = str(exc.value)
        assert str(os.getpid()) in msg
        assert "held" in msg


def test_different_repos_dont_collide(tmp_path):
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    with acquire_repo_lock(repo_a, run_id="A"):
        # Other repo's lock is independent — should acquire fine.
        with acquire_repo_lock(repo_b, run_id="B"):
            assert read_lock_info(repo_a).run_id == "A"
            assert read_lock_info(repo_b).run_id == "B"


def test_lock_path_is_stable_per_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    p1 = lock_path_for(repo)
    p2 = lock_path_for(str(repo))
    assert p1 == p2
    assert p1.name.endswith(".lock")


def test_release_on_exception(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(RuntimeError):
        with acquire_repo_lock(repo, run_id="x"):
            raise RuntimeError("boom")
    # Should be re-acquirable after the inner exception
    with acquire_repo_lock(repo, run_id="y"):
        info = read_lock_info(repo)
        assert info.run_id == "y"


def _hold_lock_and_die(repo_path: str, lock_dir_path: str, run_id: str):
    """Subprocess helper: acquire lock, write a marker, exit (releasing lock)."""
    import os as _os
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    # Re-route lock dir for the subprocess
    from luxe import locks
    locks.lock_dir = lambda _ld=Path(lock_dir_path): _ld  # type: ignore
    from luxe.locks import acquire_repo_lock
    with acquire_repo_lock(repo_path, run_id=run_id):
        Path(lock_dir_path + "/.acquired").write_text("ok")
        # Exit immediately while holding the lock — OS releases it on close.


def test_lock_released_on_holder_death(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    lockdir = tmp_path / "locks"
    p = Process(target=_hold_lock_and_die,
                args=(str(repo), str(lockdir), "child"))
    p.start()
    p.join(timeout=10)
    assert p.exitcode == 0
    # The child has exited — flock auto-released. We should be able to acquire.
    with acquire_repo_lock(repo, run_id="parent"):
        info = read_lock_info(repo)
        assert info.run_id == "parent"
