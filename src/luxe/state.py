"""Durable state storage (`.luxe/luxe_state.json`) with concurrency locking.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional
from pydantic import BaseModel, Field


class TaskPhaseState(BaseModel):
    phase_id: int
    title: str
    description: str
    status: str = "pending"  # "pending", "in_progress", "completed", "failed"
    validation_command: Optional[str] = None


class LuxeState(BaseModel):
    schema_version: int = 1
    goal: str = ""
    current_phase_index: int = 0
    next_action: str = ""
    verified_findings: list[str] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)
    current_blocker: Optional[str] = None
    known_findings: list[str] = Field(default_factory=list)
    phases: list[TaskPhaseState] = Field(default_factory=list)


def get_luxe_dir(repo_path: str | Path) -> Path:
    return Path(repo_path).resolve() / ".luxe"


def get_state_path(repo_path: str | Path) -> Path:
    return get_luxe_dir(repo_path) / "luxe_state.json"


def get_state_bak_path(repo_path: str | Path) -> Path:
    return get_luxe_dir(repo_path) / "luxe_state.json.bak"


def get_lock_path(repo_path: str | Path) -> Path:
    return get_luxe_dir(repo_path) / "luxe_state.json.lock"


class StateLockError(RuntimeError):
    pass


@contextmanager
def acquire_state_lock(repo_path: str | Path) -> Generator[None, None, None]:
    """Acquire exclusive file lock on the luxe state file."""
    luxe_dir = get_luxe_dir(repo_path)
    luxe_dir.mkdir(parents=True, exist_ok=True)
    lock_file = get_lock_path(repo_path)

    fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise StateLockError("Failed to acquire luxe_state lock; another run is active.") from e
            raise
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def load_state(repo_path: str | Path) -> LuxeState:
    """Load the state from the JSON file, falling back to backup if corrupted."""
    state_file = get_state_path(repo_path)
    state_bak = get_state_bak_path(repo_path)

    # Try reading main state file
    if state_file.is_file():
        try:
            with open(state_file, "r") as f:
                data = json.load(f)
            return LuxeState.model_validate(data)
        except Exception:
            # Main corrupted, try backup
            if state_bak.is_file():
                try:
                    with open(state_bak, "r") as f:
                        data = json.load(f)
                    # Restore from backup
                    shutil.copy2(state_bak, state_file)
                    return LuxeState.model_validate(data)
                except Exception:
                    pass

    return LuxeState()


def write_state(repo_path: str | Path, state: LuxeState) -> None:
    """Write the state to the main JSON file and the backup mirror."""
    luxe_dir = get_luxe_dir(repo_path)
    luxe_dir.mkdir(parents=True, exist_ok=True)

    state_file = get_state_path(repo_path)
    state_bak = get_state_bak_path(repo_path)

    # Temporary file for atomic write
    temp_file = luxe_dir / f"luxe_state.tmp.{os.getpid()}"
    try:
        with open(temp_file, "w") as f:
            f.write(state.model_dump_json(indent=2))
            f.flush()
            os.fsync(f.fileno())
        # Atomically rename to main state file
        if state_file.is_file():
            shutil.copy2(state_file, state_bak)
        os.replace(temp_file, state_file)
        # Copy to backup
        shutil.copy2(state_file, state_bak)
    except Exception:
        if temp_file.is_file():
            os.remove(temp_file)
        raise
