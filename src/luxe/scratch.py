"""Scratch-file classification for SWE-bench image exec (2026-11-20/21).

Under `run.py --image-exec` the model may create throwaway reproducers. Two
consumers need the same answer to "is this added path scratch?":

- `benchmarks/swebench/adapter.strip_scratch` deletes scratch before the
  prediction patch is extracted;
- the agent loop does not let a scratch write arm the post-write idle exit
  (`agents.sdd` § "Post-write idle exit under image exec").

Rules, for a path the run ADDED (never a tracked file the model edited):
1. its basename or path matches `SCRATCH_GLOBS`, or it is a `*.py` directly at
   the repo root (`is_scratch`);
2. it lives under a top-level directory that does not exist at base_commit and
   holds no `__init__.py` (`test_repro/`, `tmp/pylint_test/`, `.a/`; the
   image_exec follow-up found 5 such predictions, all pylint-6528).
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

SCRATCH_GLOBS = (
    "repro*.py", "reproduce*.py", "reproducer*.py", "scratch*", "debug_*.py",
    "tmp_*", "verify_*.py", "*_verify.py", "test_fix*.py", "test_*_fix.py",
    "test_encoded_*.py", "test_*_time.py", "check_*.py",
)


def is_scratch(path: str) -> bool:
    """Name rule only (rule 1)."""
    name = path.rsplit("/", 1)[-1]
    if "/" not in path and name.endswith(".py"):
        return True
    return any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(path, g)
               for g in SCRATCH_GLOBS)


def _top_dir_in_base(repo: Path, base_commit: str, top: str) -> bool:
    from luxe.secrets import scrubbed_env
    r = subprocess.run(["git", "cat-file", "-e", f"{base_commit}:{top}"],
                       cwd=repo, capture_output=True, timeout=30, env=scrubbed_env())
    return r.returncode == 0


def in_new_top_dir(path: str, repo: Path | str, base_commit: str) -> bool:
    """Rule 2: under a top-level dir absent at base_commit, with no package."""
    if "/" not in path:
        return False
    repo = Path(repo)
    top = path.split("/", 1)[0]
    if _top_dir_in_base(repo, base_commit, top):
        return False
    d = repo / top
    if d.is_dir():
        for _, _, files in os.walk(d):
            if "__init__.py" in files:
                return False
    return True


def relative_to_repo(path: str, repo: Path | str) -> str | None:
    """A model-supplied path as repo-relative posix, or None if outside."""
    root = Path(repo).resolve()
    p = Path(path)
    p = (p if p.is_absolute() else root / p).resolve()
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return None


def is_scratch_at(path: str, repo: Path | str, base_commit: str) -> bool:
    """Rule 1 or rule 2 for a repo-relative path."""
    return is_scratch(path) or in_new_top_dir(path, repo, base_commit)
