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


@pytest.fixture(autouse=True)
def _reset_tool_sizing_globals():
    """`tools/fs.py`'s sizing toggles are PROCESS-GLOBAL, and since chat's read
    budget went default-ON (2026-08-24,
    `acceptance/chat_bigread_2026_08_24/REPORT.md`) every `prepare_turn` call in
    the suite leaves one set — which is how a chat-seam test silently moved
    `read_limit()` under a later `test_read_file_large.py` assertion about the
    fixed 256 KB cap. Reset after each test so module order stays irrelevant."""
    yield
    from luxe.tools import fs as _fs
    _fs.set_read_budget(None)
    _fs.set_large_file_notes(False)


@pytest.fixture
def config_path() -> Path:
    return Path(__file__).parent.parent / "configs" / "single_64gb.yaml"


@pytest.fixture
def stub_public_dns(monkeypatch):
    """Answer every hostname lookup with a public address, never real DNS.

    The web egress guard resolves names before it allows them; tests that
    only need "a public name" must not depend on the network or the host's
    resolver. IP literals still go to the real getaddrinfo (no lookup
    happens for those). A test that needs a specific answer patches
    `socket.getaddrinfo` again on top of this.
    """
    import ipaddress
    import socket

    real = socket.getaddrinfo

    def _stub(host, port, *a, **k):
        name = host.decode() if isinstance(host, bytes) else str(host)
        try:
            ipaddress.ip_address(name)
            return real(host, port, *a, **k)
        except ValueError:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                     "", ("93.184.216.34", int(port or 80)))]

    monkeypatch.setattr(socket, "getaddrinfo", _stub)
