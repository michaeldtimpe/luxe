"""Tests for the startup venv-mismatch warning."""

from __future__ import annotations

import sys
from pathlib import Path

from luxe_cli import venv_check


def test_returns_none_when_no_virtual_env(monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("LUXE_NO_VENV_CHECK", raising=False)
    assert venv_check.warn_if_venv_mismatch() is None


def test_returns_none_when_active_venv_matches_runtime(monkeypatch):
    """Running luxe inside its own venv should be silent."""
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    monkeypatch.delenv("LUXE_NO_VENV_CHECK", raising=False)
    assert venv_check.warn_if_venv_mismatch() is None


def test_silenced_by_env_var(monkeypatch, tmp_path):
    """Even with a clear mismatch, LUXE_NO_VENV_CHECK suppresses the warning."""
    fake_venv = tmp_path / "other-venv"
    (fake_venv / "bin").mkdir(parents=True)
    (fake_venv / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("VIRTUAL_ENV", str(fake_venv))
    monkeypatch.setenv("LUXE_NO_VENV_CHECK", "1")
    assert venv_check.warn_if_venv_mismatch() is None


def test_returns_none_when_venv_path_missing_python(monkeypatch, tmp_path):
    """Don't crash when VIRTUAL_ENV points at something without bin/python."""
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "nonexistent"))
    monkeypatch.delenv("LUXE_NO_VENV_CHECK", raising=False)
    assert venv_check.warn_if_venv_mismatch() is None


def test_warns_when_user_venv_missing_modules(monkeypatch, tmp_path):
    """Mock a user venv whose probe reports missing deps."""
    fake_venv = tmp_path / "user-venv"
    (fake_venv / "bin").mkdir(parents=True)
    fake_python = fake_venv / "bin" / "python"
    fake_python.write_text("placeholder")
    monkeypatch.setenv("VIRTUAL_ENV", str(fake_venv))
    monkeypatch.delenv("LUXE_NO_VENV_CHECK", raising=False)

    monkeypatch.setattr(
        venv_check,
        "_missing_modules",
        lambda _python: ["httpx", "pydantic"],
    )

    msg = venv_check.warn_if_venv_mismatch()
    assert msg is not None
    assert "httpx" in msg
    assert "pydantic" in msg
    assert "uv pip install -e" in msg
    assert "LUXE_NO_VENV_CHECK" in msg


def test_silent_when_user_venv_has_all_modules(monkeypatch, tmp_path):
    """User has luxe deps in their venv → no warning, even on mismatch."""
    fake_venv = tmp_path / "user-venv"
    (fake_venv / "bin").mkdir(parents=True)
    (fake_venv / "bin" / "python").write_text("placeholder")
    monkeypatch.setenv("VIRTUAL_ENV", str(fake_venv))
    monkeypatch.delenv("LUXE_NO_VENV_CHECK", raising=False)

    monkeypatch.setattr(venv_check, "_missing_modules", lambda _python: [])

    assert venv_check.warn_if_venv_mismatch() is None


def test_missing_modules_against_real_interpreter():
    """Smoke: probe the running interpreter — should report nothing missing
    since we're already importing luxe_cli (which needs all of these)."""
    missing = venv_check._missing_modules(Path(sys.executable))
    assert missing == []
