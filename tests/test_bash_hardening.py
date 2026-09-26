"""Allowlisted bash: operator detection and timeout teardown (2026-09-26).

- `_validate_command` tokenized with `shlex.split`, which only splits on
  whitespace, so an operator glued to a word (`echo a;touch X`, `a&&b`,
  `echo hi>X`) or a bare newline ran a SECOND, non-allowlisted command.
  It now tokenizes with shell punctuation split out and rejects unquoted
  newlines. `shlex.shlex`'s default `#` comment handling is also switched
  off — with it, `echo a#;touch X` tokenized as just `echo a`.
- The bench-path timeout killed only the direct child; anything it spawned
  (a test server, a watcher) survived the timeout, orphaned, still running
  in the repo.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

from luxe.tools import fs, shell


@pytest.fixture
def repo(tmp_path: Path):
    fs.set_repo_root(tmp_path)
    yield tmp_path
    fs._REPO_ROOT = None


@pytest.mark.parametrize("cmd", [
    "echo a;touch X",
    "echo a&&touch X",
    "echo a||touch X",
    "echo a|touch X",
    "echo hi>X",
    "echo hi>>X",
    "cat<X",
    "echo a&touch X",
    "echo a\ntouch X",
    "echo a\rtouch X",
    "echo a#;touch X",
    "echo a 2>&1",
])
def test_glued_operators_and_newlines_are_rejected(repo, cmd):
    out, err = shell._bash({"command": cmd})
    assert out == "" and err, f"{cmd!r} was allowed"
    assert not (repo / "X").exists()


@pytest.mark.parametrize("cmd", [
    'grep "a|b" f',
    "grep 'x;y' f",
    'grep -E "(a|b)" f',
    "sed -n '1,5p' f",
    'pytest -k "a and not b"',
    "git log --format='%h|%s'",
    'python -c "import os\nprint(1)"',   # newline INSIDE quotes is an argument
    "echo a#b",
    "ls -la",
])
def test_quoted_punctuation_and_plain_commands_still_pass(cmd):
    tokens, err = shell._validate_command(cmd)
    assert err is None, err
    assert tokens


@pytest.mark.parametrize("cmd", ["echo $(touch X)", "echo `touch X`",
                                 "echo a$(touch X)b"])
def test_command_substitution_still_rejected(repo, cmd):
    out, err = shell._bash({"command": cmd})
    assert err and "substitution not allowed" in err
    assert not (repo / "X").exists()


def test_validator_messages_unchanged_for_the_classic_forms():
    _, err = shell._validate_command("cat foo && rm -rf /")
    assert err.startswith("Shell chain/redirect operators not allowed: ['&&']")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_kills_the_whole_process_group(repo, monkeypatch):
    monkeypatch.setattr(shell, "_TIMEOUT", 1)
    script = ("import subprocess,time; "
              "p=subprocess.Popen(['sleep','30']); "
              "open('child.pid','w').write(str(p.pid)); "
              "time.sleep(30)")
    t0 = time.monotonic()
    out, err = shell._bash({"command": f'python -c "{script}"'})
    elapsed = time.monotonic() - t0
    assert err == "Command timed out after 1s"
    assert elapsed < 10, f"bash blocked {elapsed:.1f}s past a 1s timeout"
    pid = int((repo / "child.pid").read_text())
    deadline = time.monotonic() + 3
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert not _alive(pid), "grandchild survived the timeout"
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)
