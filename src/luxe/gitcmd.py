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

Both forms pin the host git config that would otherwise corrupt captured output
(`_CONFIG_PINS`) and decode with `errors="replace"`.

Not for the benchmark path's exact-flag call sites (`pr.py`, `citations.py`,
`spec_validator.py`) — those construct their argv deliberately and stay raw
(they pin the same config themselves, plus the diff-format settings their
parsers depend on).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


#: Host git config that would corrupt CAPTURED output, pinned off at every
#: invocation (2026-08-12). `color.ui=always` in a user's ~/.gitconfig makes
#: git emit ANSI escapes even when stdout is a pipe — every consumer here
#: parses that output, and one of them grades a benchmark. A pager configured
#: to something that isn't `cat` can wedge or reformat. `--no-pager` is
#: belt-and-braces with `core.pager` for the versions that consult only one.
#: These are git's own defaults for a non-tty, so a default-config host sees
#: no change at all.
_CONFIG_PINS: tuple[str, ...] = ("-c", "color.ui=false", "-c", "core.pager=cat",
                                 "--no-pager")

#: Text decoding for every git call: a repo can hold a filename or a commit
#: message that is not valid UTF-8, and one bad byte must not discard the whole
#: output with a UnicodeDecodeError (`luxe.fswalk` sets the same precedent).
_DECODE = {"text": True, "errors": "replace"}

#: The pins for a call whose DIFF TEXT is parsed, for the raw call sites that
#: build their own argv (`pr.diff_against_base`,
#: `spec_validator._added_lines_from_diff`) — they live here, in the neutral
#: module, rather than in either caller. `diff.noprefix` and
#: `diff.mnemonicprefix` rewrite the `+++ b/<path>` header both parsers key on,
#: which would zero a grade with no error anywhere. Pair with `--no-ext-diff`
#: in the argv (`diff.external` replaces git's diff wholesale) and `parse_env()`.
DIFF_PARSE_PINS: tuple[str, ...] = (
    "-c", "color.ui=false",
    "-c", "diff.noprefix=false",
    "-c", "diff.mnemonicprefix=false",
    "-c", "core.pager=cat",
    "--no-pager",
)


def parse_env() -> dict[str, str]:
    """Environment for a git call whose output is parsed: no system config
    (the `-c` pins cover ~/.gitconfig and the repo, `GIT_CONFIG_NOSYSTEM`
    covers /etc/gitconfig) and no credential prompt on an inherited stdin."""
    import os

    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run(repo: str | Path, *args: str,
        timeout: float | None = None) -> subprocess.CompletedProcess:
    """`git -C <repo> <args…>`, output captured as text.

    A missing or non-repo `repo` returns a non-zero CompletedProcess rather
    than raising — git handles the path itself.
    """
    return subprocess.run(["git", "-C", str(repo), *_CONFIG_PINS, *args],
                          capture_output=True, timeout=timeout, **_DECODE)


def run_in(repo: str | Path, *args: str,
           timeout: float | None = None) -> subprocess.CompletedProcess:
    """`git <args…>` with `cwd=<repo>`, output captured as text.

    Raises `FileNotFoundError` when `repo` does not exist OR when git is not on
    PATH — callers that distinguish the two must do so some other way.
    """
    return subprocess.run(["git", *_CONFIG_PINS, *args], cwd=str(repo),
                          capture_output=True, timeout=timeout,
                          check=False, **_DECODE)


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
