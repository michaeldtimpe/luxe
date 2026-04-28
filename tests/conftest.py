"""Shared test fixtures."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Create a minimal repo structure for testing."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n\n'
        'def add(a: int, b: int) -> int:\n    return a + b\n'
    )
    (tmp_path / "src" / "utils.py").write_text(
        'import os\n\ndef get_env(key: str) -> str:\n    return os.environ.get(key, "")\n'
    )
    (tmp_path / "README.md").write_text("# Test Repo\n\nA test repository.\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\nversion = "0.1.0"\n')
    return tmp_path


@pytest.fixture
def config_path() -> Path:
    return Path(__file__).parent.parent / "configs" / "pipeline.yaml"
