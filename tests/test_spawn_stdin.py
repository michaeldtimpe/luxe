"""Non-tool spawns don't inherit luxe's stdin either (2026-09-26).

tools.sdd's rule — a child that reads stdin reads LUXE's: under a piped
parent (script-launched bench, CI, `printf … | luxe chat`) it drains the
session's queued input, under a TTY it blocks — was applied to the tool
spawns on 2026-08-12. These three were missed: `gitcmd.run`/`run_in` (every
git call outside the benchmark's raw sites), `repo_index._git_recent_files`
(which also had no timeout), and `secrets._from_keychain`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe import gitcmd, repo_index, secrets


@pytest.fixture
def spy(monkeypatch):
    seen: list[dict] = []

    def fake(cmd, **kw):
        seen.append(kw)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake)
    return seen


def test_gitcmd_run_uses_devnull(spy, tmp_path: Path):
    gitcmd.run(tmp_path, "status")
    assert spy[-1].get("stdin") is subprocess.DEVNULL


def test_gitcmd_run_in_uses_devnull(spy, tmp_path: Path):
    gitcmd.run_in(tmp_path, "status")
    assert spy[-1].get("stdin") is subprocess.DEVNULL


def test_repo_index_recent_files_devnull_and_bounded(spy, tmp_path: Path):
    repo_index._git_recent_files(tmp_path)
    assert spy[-1].get("stdin") is subprocess.DEVNULL
    assert spy[-1].get("timeout")


def test_repo_index_recent_files_timeout_is_empty_not_a_crash(monkeypatch, tmp_path):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
    monkeypatch.setattr(subprocess, "run", slow)
    assert repo_index._git_recent_files(tmp_path) == []


def test_keychain_lookup_uses_devnull(spy):
    secrets._from_keychain("OMLX_API_KEY_TEST")
    assert spy[-1].get("stdin") is subprocess.DEVNULL
