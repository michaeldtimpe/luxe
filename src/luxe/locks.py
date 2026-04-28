"""Per-repo concurrency locks — prevents two luxe runs trampling each other.

The CLI (luxe maintain) and the MCP server (luxe_maintain via Claude Desktop)
both orchestrate model swaps, build BM25/AST indices, and mutate run state on
the same repo. Without a lock, parallel runs corrupt state and thrash oMLX.

Design (per plan §5/§8):
- One lockfile per repo at ~/.luxe/locks/<sha256(repo_abs_path)>.lock.
- POSIX `flock` (LOCK_EX | LOCK_NB) — auto-releases when the holder dies.
- Lockfile JSON contains run_id, PID, started_at for diagnostics.
- A second invocation fast-fails with the holding PID and start time.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


def lock_dir() -> Path:
    return Path.home() / ".luxe" / "locks"


def lock_path_for(repo_path: str | Path) -> Path:
    abs_path = str(Path(repo_path).expanduser().resolve())
    digest = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()
    return lock_dir() / f"{digest}.lock"


@dataclass
class LockInfo:
    pid: int
    run_id: str
    started_at: float
    repo_path: str


class LockHeld(RuntimeError):
    def __init__(self, info: LockInfo, lock_path: Path):
        self.info = info
        self.lock_path = lock_path
        elapsed = max(0.0, time.time() - info.started_at)
        msg = (
            f"another luxe run is active on this repo "
            f"(PID {info.pid}, run_id {info.run_id}, started {elapsed:.0f}s ago, "
            f"lock at {lock_path})"
        )
        super().__init__(msg)


def _read_lock_info(p: Path) -> LockInfo | None:
    try:
        data = json.loads(p.read_text())
        return LockInfo(
            pid=int(data.get("pid", 0)),
            run_id=str(data.get("run_id", "")),
            started_at=float(data.get("started_at", 0.0)),
            repo_path=str(data.get("repo_path", "")),
        )
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno != errno.ESRCH
    return True


@contextmanager
def acquire_repo_lock(repo_path: str | Path, run_id: str):
    """Acquire an exclusive lock for `repo_path`. Raises LockHeld if held.

    The lockfile is written with the current PID + run_id + start timestamp,
    truncated and rewritten on acquisition so stale info from a crashed
    previous holder doesn't mislead diagnostics.

    Auto-releases on context exit (fd close → flock LOCK_UN). If the process
    dies while holding the lock, the OS releases it automatically.
    """
    lock_dir().mkdir(parents=True, exist_ok=True)
    p = lock_path_for(repo_path)

    # Open or create the lockfile. O_RDWR so we can both lock and write info.
    fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
            # Lock held by another process — read the stored info to surface PID.
            info = _read_lock_info(p)
            if info is None:
                # Couldn't read info — synthesize a minimal record.
                info = LockInfo(pid=-1, run_id="?", started_at=time.time(),
                                repo_path=str(Path(repo_path).resolve()))
            # If the PID stored isn't alive, the lock is stale — try once more.
            # (flock already released by the dead process, so the second
            # attempt should succeed.)
            if info.pid > 0 and not _is_pid_alive(info.pid):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise LockHeld(info, p) from None
            else:
                raise LockHeld(info, p) from None

        # We own the lock — write our info.
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        info_bytes = json.dumps({
            "pid": os.getpid(),
            "run_id": run_id,
            "started_at": time.time(),
            "repo_path": str(Path(repo_path).expanduser().resolve()),
        }).encode("utf-8")
        os.write(fd, info_bytes)
        os.fsync(fd)

        try:
            yield p
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


def read_lock_info(repo_path: str | Path) -> LockInfo | None:
    """Inspect the current lock holder (if any) without acquiring."""
    p = lock_path_for(repo_path)
    if not p.is_file():
        return None
    return _read_lock_info(p)
