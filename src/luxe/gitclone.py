"""Bounded `git clone` — the one place luxe shells out to the network.

Two callers had near-identical clone code (`cli._resolve_repo` and
`gitkit.runner._clone`) and neither bounded it, so a hung transfer blocked
forever with no output. This module is the shared, bounded implementation.

The bound is on PROGRESS, not on duration — the same distinction B6 forced in
`backend.py`. A wall-clock cap alone is the wrong tool here: a legitimately
large clone over a slow link is not a failure, and killing it at an arbitrary
minute mark would be its own bug. git already knows how to detect a stalled
transfer (`http.lowSpeedLimit` / `http.lowSpeedTime`), so we use that as the
primary guard and keep a generous wall cap only as a backstop for the cases
low-speed detection can't see (a wedged helper process, a hung DNS lookup).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Abort when the transfer sits below this many bytes/sec for this many seconds.
# 1 KB/s for a minute is far under any working connection while being clearly
# distinguishable from "slow but moving".
_LOW_SPEED_BYTES = 1000
_LOW_SPEED_SECONDS = 60
# Backstop only. Nothing legitimate should reach it: both call sites clone
# shallow (`--depth=1`) or blobless (`--filter=blob:none`).
_WALL_CAP_S = 1800.0


def clone_env() -> dict[str, str]:
    """Environment for a non-interactive clone.

    `GIT_TERMINAL_PROMPT=0` matters: without it a private URL makes git block
    on a credential prompt, which is indistinguishable from a slow network and
    is exactly the kind of silent hang this module exists to prevent.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "")
    return env


def clone_argv(url: str, dest: Path | str, *, full_history: bool) -> list[str]:
    """The full `git clone` argv, stall guards included."""
    depth = ["--filter=blob:none"] if full_history else ["--depth=1"]
    return [
        "git",
        "-c", f"http.lowSpeedLimit={_LOW_SPEED_BYTES}",
        "-c", f"http.lowSpeedTime={_LOW_SPEED_SECONDS}",
        "clone", *depth, url, str(dest),
    ]


def clone(url: str, dest: Path | str, *, full_history: bool) -> tuple[bool, str]:
    """Clone `url` into `dest`. Returns `(ok, message)`; never raises.

    `message` is empty on success and carries the failure reason otherwise, so
    both call sites can render it however they like.
    """
    try:
        proc = subprocess.run(
            clone_argv(url, dest, full_history=full_history),
            capture_output=True, text=True, env=clone_env(), timeout=_WALL_CAP_S,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"clone exceeded {_WALL_CAP_S:.0f}s and was killed. The transfer was "
            f"still alive (a stall under {_LOW_SPEED_BYTES} B/s for "
            f"{_LOW_SPEED_SECONDS}s would have aborted it sooner), so this is "
            f"most likely a very large repo or a wedged credential helper."
        )
    except OSError as e:
        return False, f"could not run git: {e}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()
    return True, ""


def resolve_repo(repo: str, *, full_history: bool = False) -> str:
    """Resolve a repo argument to a local path. Clones if it's a URL.

    `full_history=True` clones with `--filter=blob:none` (full commit history,
    lazy blobs) so commit-cadence/health analysis is meaningful; the default
    `--depth=1` shallow clone is fine for code-only analysis.

    Moved verbatim from `cli._resolve_repo` (2026-08-05, deferred-list #6):
    repo-or-URL resolution belongs next to the bounded clone it uses, and its
    old home forced `chat/launch.py` and `maintain.py` to reach back up into
    `cli`. It keeps its CLI manners — prints via rich and `sys.exit(1)`s on
    failure — because every caller is a command entry point.
    """
    import sys
    import tempfile

    from rich.console import Console

    console = Console()

    p = Path(repo).expanduser().resolve()
    if p.is_dir():
        return str(p)

    if repo.startswith(("http://", "https://", "git@")):
        clone_dir = Path(tempfile.mkdtemp(prefix="luxe_"))
        console.print(f"[dim]Cloning {repo} → {clone_dir}[/]")
        ok, err = clone(repo, clone_dir, full_history=full_history)
        if not ok:
            console.print(f"[red]Clone failed:[/] {err}")
            sys.exit(1)
        return str(clone_dir)

    console.print(f"[red]Not a directory or repo URL:[/] {repo}")
    sys.exit(1)
