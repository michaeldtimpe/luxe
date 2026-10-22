"""Subprocess layer shared by grade.py and gates.py.

`run` reports a timeout or a missing binary as rc 124 / 127 instead of raising;
`run_git` is for git commands the grader depends on, and raises GitRunError
on any rc outside `ok_codes` (maintain_suite.sdd: a git failure during
grading is a harness ERROR, never a verdict).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run(cmd: list[str], cwd: str | Path | None = None,
        timeout: float | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True,
                              check=False, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "[timed out]"
    except FileNotFoundError as e:
        return 127, str(e)


class GitRunError(RuntimeError):
    """A git command the GRADER depends on failed.

    Raised instead of returning an empty-diff-shaped value ([] / (0, 0) / ""),
    which scored every diff-dependent requirement as "the model changed
    nothing" — a wrong verdict wearing the shape of a real one (the same
    failure-looks-like-no-diff class fixed on the tool surface 2026-08-12).
    run.py's per-fixture handler converts this into an ERRORED fixture, which
    the scoreboard already distinguishes from FAILED, so an index.lock race, a
    corrupt base_sha, or a missing git binary can never masquerade as a model
    regression. maintain_suite.sdd pins this.
    """


def run_git(cmd: list[str], cwd: str | Path | None = None,
            timeout: float | None = 60,
            ok_codes: tuple[int, ...] = (0,)) -> tuple[int, str]:
    """`run` for git commands whose failure must never grade as a verdict."""
    rc, out = run(cmd, cwd=cwd, timeout=timeout)
    if rc not in ok_codes:
        tail = (out or "").strip()[-300:]
        raise GitRunError(
            f"{' '.join(cmd)} (cwd={cwd}) exited {rc}: {tail or '<no output>'}")
    return rc, out
