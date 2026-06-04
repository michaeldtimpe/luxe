import json
import os
import shutil
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from luxe.state import (
    LuxeState,
    TaskPhaseState,
    load_state,
    write_state,
    acquire_state_lock,
    StateLockError,
)
from luxe.chat.commands import _clear, CommandContext
from luxe.agents.loop import run_validation


def test_state_load_write_backup():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Test default load
        state = load_state(tmp_path)
        assert state.goal == ""
        assert len(state.phases) == 0

        # Write state
        state.goal = "Test Goal"
        state.phases = [
            TaskPhaseState(phase_id=1, title="Phase 1", description="Desc 1", status="in_progress")
        ]
        write_state(tmp_path, state)

        # Load again
        loaded = load_state(tmp_path)
        assert loaded.goal == "Test Goal"
        assert len(loaded.phases) == 1
        assert loaded.phases[0].title == "Phase 1"

        # Corrupt main file to test backup recovery
        state_file = tmp_path / ".luxe" / "luxe_state.json"
        with open(state_file, "w") as f:
            f.write("corrupted json")

        recovered = load_state(tmp_path)
        assert recovered.goal == "Test Goal"
        assert len(recovered.phases) == 1


def test_concurrency_lock():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Acquire lock first time
        with acquire_state_lock(tmp_path):
            # Attempt to acquire concurrently should fail
            with pytest.raises(StateLockError):
                with acquire_state_lock(tmp_path):
                    pass


def test_run_validation():
    # True for successful commands
    assert run_validation("echo 'Hello'") is True
    # False for failing commands
    assert run_validation("exit 1") is False


def test_clear_command_resets_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Setup active session and state
        state = LuxeState(goal="Old Goal")
        write_state(tmp_path, state)

        # Mock CommandContext
        ctx = MagicMock()
        ctx.session.repo_path = str(tmp_path)
        ctx.session.project_hash = "mock_hash"
        ctx.session.turns = []
        ctx.slots.slot_models.return_value = {}

        # Call clear command
        _clear(None, ctx)

        # Verify state is cleared/reset
        cleared_state = load_state(tmp_path)
        assert cleared_state.goal == ""
