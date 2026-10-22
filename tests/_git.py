"""The one `git` runner tests use to build and inspect throwaway repos.

Fifteen test modules each carried their own copy of a three-line
`subprocess.run(["git", ...])` helper, differing only in whether a failure
raises and whether the output is stripped. Those two differences are the
keywords here; import it under the module's old local name so call sites stay
put (`from tests._git import git as _git`).

It sets no environment and no config of its own: identity, signing and the
default branch come from the hermetic `~/.gitconfig` that conftest's
`_hermetic_home` writes for every test (unsigned, fixed identity), plus
whatever repo-local config the test sets.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(repo: Path | str, *args: str, check: bool = True,
        strip: bool = True) -> str:
    """Run `git *args` in `repo` and return its stdout (stderr captured).

    `check=False` returns whatever stdout a failing command produced instead
    of raising; `strip=False` keeps the output byte-for-byte, for tests that
    compare against raw git.
    """
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=check)
    return proc.stdout.strip() if strip else proc.stdout
