"""Bounded `git clone` (B1).

Both clone sites used to shell out with no bound at all, so a hung transfer
blocked forever with no output. The guard is on PROGRESS, not duration — a
large clone over a slow link is not a failure, and a bare wall-clock cap would
make it one. git's own low-speed detection does the real work; the subprocess
timeout is a backstop for what it can't see (wedged helper, hung DNS).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from luxe import gitclone


def test_argv_carries_the_stall_guards():
    argv = gitclone.clone_argv("https://x/y.git", "/tmp/dest", full_history=False)
    joined = " ".join(argv)
    assert "http.lowSpeedLimit=1000" in joined
    assert "http.lowSpeedTime=60" in joined
    # The guards must precede the subcommand — `git -c ... clone`, not
    # `git clone -c ...`, which git would reject.
    assert argv.index("clone") > argv.index("-c")


def test_shallow_by_default_blobless_for_full_history():
    shallow = gitclone.clone_argv("u", "d", full_history=False)
    full = gitclone.clone_argv("u", "d", full_history=True)
    assert "--depth=1" in shallow and "--filter=blob:none" not in shallow
    assert "--filter=blob:none" in full and "--depth=1" not in full
    # Preserved from the previous inline implementations, so the transfer
    # profile of an existing clone doesn't change.
    assert shallow[-2:] == ["u", "d"]


def test_env_disables_credential_prompting():
    """A private URL must fail, not block on a prompt that never comes.

    An interactive credential prompt is indistinguishable from a slow network
    and is exactly the silent hang this module exists to prevent.
    """
    env = gitclone.clone_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == ""


def test_failure_returns_reason_without_raising(monkeypatch):
    def boom(*a, **kw):
        return subprocess.CompletedProcess(a, 128, stdout="", stderr="fatal: no such repo")

    monkeypatch.setattr(gitclone.subprocess, "run", boom)
    ok, err = gitclone.clone("https://x/y.git", "/tmp/d", full_history=False)
    assert ok is False
    assert "no such repo" in err


def test_timeout_is_reported_not_raised(monkeypatch):
    """The backstop must surface a diagnosis, not a traceback."""
    def hang(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git clone", timeout=kw.get("timeout", 0))

    monkeypatch.setattr(gitclone.subprocess, "run", hang)
    ok, err = gitclone.clone("https://x/y.git", "/tmp/d", full_history=False)
    assert ok is False
    assert "exceeded" in err and "wedged credential helper" in err


def test_wall_cap_is_passed_to_subprocess(monkeypatch):
    seen: dict = {}

    def capture(*a, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(a, 0, stdout="", stderr="")

    monkeypatch.setattr(gitclone.subprocess, "run", capture)
    ok, _ = gitclone.clone("https://x/y.git", "/tmp/d", full_history=False)
    assert ok is True
    assert seen["timeout"] == gitclone._WALL_CAP_S
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_missing_git_binary_degrades(monkeypatch):
    monkeypatch.setattr(gitclone.subprocess, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("no git")))
    ok, err = gitclone.clone("https://x/y.git", "/tmp/d", full_history=False)
    assert ok is False and "could not run git" in err


def test_real_clone_of_a_local_repo_succeeds(tmp_path: Path):
    """End-to-end against a real git repo — the argv has to actually work.

    Guards against a malformed `-c` placement or flag that only unit tests
    with a stubbed subprocess would miss.
    """
    src = tmp_path / "src"
    src.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(src), *args], check=True,
                       capture_output=True, timeout=30)
    (src / "f.txt").write_text("hi\n")
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True,
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(src), "commit", "-qm", "c"], check=True,
                   capture_output=True, timeout=30)

    dest = tmp_path / "dest"
    ok, err = gitclone.clone(str(src), dest, full_history=False)
    assert ok, err
    assert (dest / "f.txt").read_text() == "hi\n"
