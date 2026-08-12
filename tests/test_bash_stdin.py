"""The shell tool inherited luxe's stdin, and decoded its output strictly.

**It read the session's input.** `_bash` spawned the command with stdin
inherited, so any command that reads stdin read LUXE's stdin. With a piped
parent — a benchmark launched from a script, CI, the headless
`printf 'msg\\n/quit\\n' | luxe chat` form the README documents — `bash("cat")`
returned the parent's queued input as its own tool output AND drained it, so
the next read of that pipe got nothing. Under a TTY the same command blocks
until the 60s (or dev-mode 600s) timeout with the user staring at a hung turn.
Both spawn sites had it: the benchmark's `subprocess.run` and the chat-only
cancellable `Popen`.

**One bad byte discarded everything.** `text=True` with strict decoding raised
`UnicodeDecodeError` out of subprocess, so a command that printed 8 KB of
readable output plus one stray byte produced a tool error and no output at all.

The same two fixes apply to the other places luxe runs a model- or
config-supplied command: `gitkit.apply._run_verify` and
`spec_validator._eval_tests_pass`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from luxe.tools import fs, shell


@pytest.fixture(autouse=True)
def set_root(tmp_repo: Path):
    fs.set_repo_root(tmp_repo)
    yield
    fs._REPO_ROOT = None


def _spy_run(monkeypatch) -> dict:
    seen: dict = {}
    real = subprocess.run

    def _spy(cmd, **kw):
        seen.update(kw)
        return real(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", _spy)
    return seen


class TestTheBenchmarkSpawn:
    def test_stdin_is_devnull(self, monkeypatch):
        seen = _spy_run(monkeypatch)
        shell.TOOL_FNS["bash"]({"command": "echo hi"})
        assert seen.get("stdin") is subprocess.DEVNULL

    def test_decoding_replaces_bad_bytes(self, monkeypatch):
        seen = _spy_run(monkeypatch)
        shell.TOOL_FNS["bash"]({"command": "echo hi"})
        assert seen.get("errors") == "replace"

    def test_env_none_is_still_passed_through(self, monkeypatch):
        """The bench default must stay `env=None` = inherit, unchanged."""
        seen = _spy_run(monkeypatch)
        shell.TOOL_FNS["bash"]({"command": "echo hi"})
        assert seen.get("env") is None

    def test_ordinary_output_is_unchanged(self):
        out, err = shell.TOOL_FNS["bash"]({"command": "echo hello"})
        assert (out, err) == ("hello\n", None)


class TestTheCancellableSpawn:
    def test_stdin_is_devnull_and_decoding_replaces(self, monkeypatch):
        seen: dict = {}
        real = subprocess.Popen

        class _Spy(real):  # type: ignore[misc]
            def __init__(self, *a, **kw):
                seen.update(kw)
                super().__init__(*a, **kw)

        monkeypatch.setattr(subprocess, "Popen", _Spy)

        class _Token:
            requested = False

        fn = shell.make_bash_fn(cancel=_Token())
        out, err = fn({"command": "echo hi"})
        assert (out, err) == ("hi\n", None)
        assert seen.get("stdin") is subprocess.DEVNULL
        assert seen.get("errors") == "replace"


class TestItNoLongerEatsTheSessionsInput:
    """End to end, in a child process whose stdin really is a pipe — the
    condition the bug needed and a pytest process cannot fake."""

    _SCRIPT = """
import sys
from luxe.tools import fs, shell
fs.set_repo_root(sys.argv[1])
out, err = shell.TOOL_FNS["bash"]({"command": "cat"})
print("TOOL_OUTPUT:" + repr(out))
print("LEFT_ON_STDIN:" + repr(sys.stdin.read()))
"""

    def test_cat_returns_nothing_and_leaves_the_pipe_alone(self, tmp_repo):
        proc = subprocess.run(
            [sys.executable, "-c", self._SCRIPT, str(tmp_repo)],
            input="SECRET-PARENT-INPUT\n", capture_output=True, text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert "TOOL_OUTPUT:''" in proc.stdout, proc.stdout
        assert "SECRET-PARENT-INPUT" in proc.stdout.split(
            "LEFT_ON_STDIN:")[1], proc.stdout


class TestNonUtf8Output:
    def test_a_bad_byte_does_not_discard_the_output(self, tmp_repo):
        (tmp_repo / "bad.bin").write_bytes(b"before \xff after\n")
        out, err = shell.TOOL_FNS["bash"]({"command": "cat bad.bin"})
        assert err is None
        assert "before" in out and "after" in out


class TestTheOtherCommandRunners:
    def test_gitkit_apply_verify(self, tmp_repo, monkeypatch):
        from luxe.gitkit import apply as apply_mod

        seen = _spy_run(monkeypatch)
        # "test" makes it look like a verify command; `true` keeps it instant.
        ok, tail = apply_mod._run_verify("true # test", tmp_repo, timeout=5)
        assert seen.get("stdin") is subprocess.DEVNULL
        assert seen.get("errors") == "replace"
        assert ok in (True, False)

    def test_spec_validator_tests_pass(self, tmp_repo, monkeypatch):
        from luxe import spec_validator
        from luxe.spec import Requirement

        seen = _spy_run(monkeypatch)
        req = Requirement(id="R1", must="it passes", done_when="exit 0",
                          kind="tests_pass", command="true")
        got = spec_validator._eval_tests_pass(req, tmp_repo)
        assert got.satisfied is True
        assert seen.get("stdin") is subprocess.DEVNULL
        assert seen.get("errors") == "replace"
