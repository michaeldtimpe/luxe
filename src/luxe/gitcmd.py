"""One place that spawns `git`.

Eight modules had their own two-to-five-line subprocess wrapper. The LAUNCH is
identical in all of them — build `["git", …]`, capture both streams as text,
bound it with a timeout — so it lives here once. What is NOT identical, and is
deliberately left at the call sites, is the ERROR POLICY: `chat.inspection`
reports the exception's `str()`, `gitkit.health` reports "git not found" /
"git timed out after Ns", `chat.status` swallows everything into `None`,
`tools.git` truncates and names the exit code. Those strings are user-visible
and each subsystem's own; folding them together would change output.

Two entry points because the two ways of pointing git at a repo are NOT
equivalent when the directory is missing:

- `run()` uses `git -C <repo>`, so a bad path comes back as a non-zero exit
  with git's message on stderr.
- `run_in()` uses `cwd=<repo>`, so a bad path raises `FileNotFoundError` /
  `NotADirectoryError` out of `Popen` — indistinguishable, at the call site,
  from git itself being absent.

Callers keep whichever form they already used. Neither function catches
anything: the exceptions are the signal.

Not for the benchmark path's exact-flag call sites (`pr.py`, `citations.py`,
`spec_validator.py`) — those construct their argv deliberately and stay raw.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run(repo: str | Path, *args: str,
        timeout: float | None = None) -> subprocess.CompletedProcess:
    """`git -C <repo> <args…>`, output captured as text.

    A missing or non-repo `repo` returns a non-zero CompletedProcess rather
    than raising — git handles the path itself.
    """
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=timeout)


def run_in(repo: str | Path, *args: str,
           timeout: float | None = None) -> subprocess.CompletedProcess:
    """`git <args…>` with `cwd=<repo>`, output captured as text.

    Raises `FileNotFoundError` when `repo` does not exist OR when git is not on
    PATH — callers that distinguish the two must do so some other way.
    """
    return subprocess.run(["git", *args], cwd=str(repo),
                          capture_output=True, text=True, timeout=timeout,
                          check=False)


def run_ok(repo: str | Path, *args: str,
           timeout: float | None = 10.0) -> tuple[bool, str]:
    """`run()` reduced to (succeeded?, text): stdout when git exited 0, else
    stderr, else the exception's own message. Everything stripped.

    The shape `chat.inspection` uses. `gitkit.health` wants the same tuple but
    with its own wording for a missing binary and a timeout, so it maps
    `run_in` itself — see the module docstring.
    """
    try:
        proc = run(repo, *args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return proc.returncode == 0, (proc.stdout if proc.returncode == 0
                                  else proc.stderr).strip()
