"""Detect a brew-managed service still running a Cellar tree brew deleted.

`brew upgrade` replaces the Cellar tree but does NOT restart a running
service, so the old process keeps executing from a directory that no longer
exists. Already-imported code keeps working, which is what makes this so
hard to spot — the server answers, the catalog lists, chat turns succeed. It
breaks only at the next LAZY import from the deleted site-packages, and the
error names whatever module happened to be imported last:

    2026-08-03  certifi's CA bundle  → `[Errno 2] No such file or directory`
                                       on every HF download (looked like a
                                       network fault)
    2026-08-04  transformers.models.qwen3_vl
                                     → `ModuleNotFoundError` when loading the
                                       fallback model (looked like a missing
                                       dependency — and the installed venv
                                       demonstrably had it)

Both cost real debugging time, and the second sat undetected on a second host
where it had quietly disabled the fallback path. The generalised rule, from
`lessons.md`:

    Any "impossible" import or file error from a brew-managed long-running
    service is the stale-process signature, whatever the errno. If the
    installed venv demonstrably has what the running process cannot import,
    the running process is not using the installed venv.

So this module answers the question directly rather than leaving it to be
rediscovered: which Cellar tree is the running process actually using, and is
it the installed one?

Local only, and never raises — `/doctor` and `luxe smoke` both run during
outages and neither may be taken down by a diagnostic.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["StaleCheck", "check_stale_service", "check_omlx"]

_CELLAR_RE = re.compile(r"/Cellar/(?P<formula>[^/]+)/(?P<version>[^/]+)")
_TIMEOUT_S = 5.0


@dataclass
class StaleCheck:
    """Verdict for one brew-managed service.

    `conclusive` is False when we could not determine the answer — the service
    is not running, `lsof` is unavailable, the formula is not brew-installed.
    That is a non-answer, not a clean bill of health: callers must not report
    "OK" on it.
    """
    formula: str
    conclusive: bool = False
    stale: bool = False
    pid: int | None = None
    running_versions: tuple[str, ...] = ()
    installed_versions: tuple[str, ...] = ()
    basis: str = ""          # "lsof" | "mtime" | ""
    reason: str = ""         # why inconclusive, when it is

    @property
    def running(self) -> str:
        return ", ".join(self.running_versions)

    @property
    def installed(self) -> str:
        return ", ".join(self.installed_versions)

    @property
    def detail(self) -> str:
        if not self.conclusive:
            return self.reason
        if self.stale:
            return (f"pid {self.pid} is running {self.running}, but "
                    f"{self.installed} is what's installed — brew replaced the "
                    f"tree underneath it")
        return f"{self.running} (matches installed)"

    @property
    def fix(self) -> str:
        return f"`brew services restart {self.formula}`" if self.stale else ""


def _brew_prefix() -> str:
    env = os.environ.get("HOMEBREW_PREFIX")
    if env:
        return env
    brew = shutil.which("brew")
    if brew:
        # <prefix>/bin/brew
        return str(Path(brew).resolve().parent.parent)
    for candidate in ("/opt/homebrew", "/usr/local"):
        if Path(candidate, "Cellar").is_dir():
            return candidate
    return "/opt/homebrew"


def _run(argv: list[str]) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def _installed_versions(formula: str) -> tuple[str, ...]:
    cellar = Path(_brew_prefix()) / "Cellar" / formula
    if not cellar.is_dir():
        return ()
    return tuple(sorted(
        p.name for p in cellar.iterdir() if p.is_dir() and not p.name.startswith(".")
    ))


def _pid_for(pattern: str) -> int | None:
    out = _run(["pgrep", "-f", pattern])
    pids = [int(x) for x in out.split() if x.strip().isdigit()]
    return pids[0] if pids else None


def _versions_from_lsof(pid: int, formula: str) -> tuple[str, ...]:
    """Cellar versions the process actually has open. `-Fn` is the machine
    format (name records only) and much cheaper than the default table."""
    out = _run(["lsof", "-p", str(pid), "-Fn"])
    found: set[str] = set()
    for m in _CELLAR_RE.finditer(out):
        if m.group("formula") == formula:
            found.add(m.group("version"))
    return tuple(sorted(found))


def _process_start_epoch(pid: int) -> float | None:
    out = _run(["ps", "-p", str(pid), "-o", "lstart="]).strip()
    if not out:
        return None
    import time
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %b %e %H:%M:%S %Y"):
        try:
            return time.mktime(time.strptime(out, fmt))
        except ValueError:
            continue
    return None


def check_stale_service(formula: str, process_pattern: str) -> StaleCheck:
    """Is the running `formula` service using the installed Cellar tree?

    Primary basis is `lsof` — definitive, because it reports the paths the
    process actually holds open. When `lsof` gives nothing usable (missing,
    permission-denied on another user's process), falls back to comparing the
    process start time against the installed tree's creation time: a tree
    created after the process started cannot be the one it is running.

    Never raises. Callers guard too, but the promise belongs here: this is a
    diagnostic, and a diagnostic that can take down `/doctor` during an outage
    is worse than no diagnostic.
    """
    try:
        return _check_stale_service(formula, process_pattern)
    except Exception as e:  # noqa: BLE001 - see docstring
        return StaleCheck(formula=formula, reason=f"check errored: {e}")


def _check_stale_service(formula: str, process_pattern: str) -> StaleCheck:
    check = StaleCheck(formula=formula)
    installed = _installed_versions(formula)
    check.installed_versions = installed
    if not installed:
        check.reason = f"{formula} is not brew-installed here"
        return check

    pid = _pid_for(process_pattern)
    if pid is None:
        check.reason = f"no running {formula} process"
        return check
    check.pid = pid

    running = _versions_from_lsof(pid, formula)
    if running:
        check.conclusive = True
        check.basis = "lsof"
        check.running_versions = running
        check.stale = any(v not in installed for v in running)
        return check

    # Fallback: a tree created after the process started cannot be in use.
    started = _process_start_epoch(pid)
    if started is None:
        check.reason = f"could not inspect pid {pid} (lsof and ps both quiet)"
        return check
    cellar = Path(_brew_prefix()) / "Cellar" / formula
    newest = 0.0
    for version in installed:
        try:
            newest = max(newest, (cellar / version).stat().st_ctime)
        except OSError:
            continue
    if not newest:
        check.reason = f"could not stat {cellar}"
        return check
    check.conclusive = True
    check.basis = "mtime"
    check.stale = newest > started
    check.running_versions = ("unknown",) if check.stale else installed
    return check


def check_omlx() -> StaleCheck:
    """The concrete case this exists for. Call only for a LOCAL endpoint — a
    remote host's process table is its own doctor's problem."""
    return check_stale_service("omlx", "omlx-server")
