"""Self-repair for the one failure luxe can fix on its own: a stale oMLX.

`luxe.staleproc` DETECTS a brew-managed server still executing from a
Cellar tree `brew upgrade` deleted. Detection alone was not enough. On
2026-09-11 (m1) `luxe smoke` printed the diagnosis, a runnable fix, and
`NOT READY` — and the fallback kit was still down at the moment it was
reached for, until a human ran the command it had just printed. The
third occurrence of the same condition (2026-08-03, 2026-08-04,
2026-09-11), each costing a person time at exactly the wrong moment.

So this module ACTS: when the stale signature is present and the
endpoint is a local, brew-installed oMLX, restart the service, wait for
`/health`, and confirm the new process runs the installed tree. Every
caller (`luxe smoke`, `luxe ready --fix`, `luxe repair`, a chat turn
that fails with the signature, `/repair`) goes through `repair_omlx`.

Invariants:

- **Two independent reasons, either suffices.** `staleproc` conclusive-
  and-stale (the process table says so), OR the error text carries the
  stale-load signature (`No module named` / `[Errno 2]` / `cannot import
  name` from a LOAD failure) — the 2026-09-11 409 bodies match even when
  `lsof` is mute. Anything else is refused: a restart is never a generic
  "try turning it off and on".
- **LOCAL, brew-installed oMLX only.** A remote host's process table is
  its own doctor's problem; a non-brew install has nothing to restart
  through `brew services`; a non-oMLX engine has no formula.
- **Never loops.** One restart per `COOLDOWN_S` per process — a server
  that is still broken after a restart is a different problem, and the
  second attempt would only hide it. Callers that retry a turn retry it
  ONCE.
- **Never raises.** Like `staleproc`: a repair path that can take down
  the session it is trying to save is worse than none.
- **Loud.** The result carries every step it took, and callers print
  them: an operator must be able to see that luxe restarted a server.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field

from luxe.staleproc import StaleCheck, _installed_versions, check_omlx

__all__ = ["RepairResult", "looks_stale", "repair_omlx", "COOLDOWN_S"]

# An "impossible" import or file error surfacing from a model LOAD. The
# body oMLX returns on the failed load names the module the deleted tree
# no longer has (`transformers.models.qwen3_vl`, `omlx.patches.mlx_lm_mtp`,
# certifi's bundle as `[Errno 2]`). lessons.md 2026-08-03/04: whatever the
# errno, this is the stale-process signature — if the installed venv has
# it and the running process cannot import it, the running process is not
# using the installed venv.
_SIGNATURE_RE = re.compile(
    r"No module named|\[Errno 2\] No such file|cannot import name|"
    r"unavailable after a previous load failure",
    re.IGNORECASE,
)

COOLDOWN_S = 300.0
_RESTART_TIMEOUT_S = 90.0
_HEALTH_WAIT_S = 60.0
_last_restart_at: float | None = None


def looks_stale(text: str) -> bool:
    """Does an error body carry the stale-load signature?"""
    return bool(text) and _SIGNATURE_RE.search(text) is not None


@dataclass
class RepairResult:
    attempted: bool = False
    ok: bool = False
    reason: str = ""                      # why we acted, or why we refused
    steps: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def detail(self) -> str:
        if not self.attempted:
            return self.reason
        head = "restarted oMLX" if self.ok else "restart did not recover oMLX"
        return f"{head} in {self.seconds:.0f}s — {self.reason}"


def _now() -> float:
    return time.monotonic()


def _in_cooldown() -> float:
    """Seconds left on the cooldown, or 0."""
    if _last_restart_at is None:
        return 0.0
    left = COOLDOWN_S - (_now() - _last_restart_at)
    return max(0.0, left)


def reset_cooldown() -> None:
    """Tests only."""
    global _last_restart_at
    _last_restart_at = None


def _restart_service(formula: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(["brew", "services", "restart", formula],
                              capture_output=True, text=True,
                              timeout=_RESTART_TIMEOUT_S, check=False)
    except FileNotFoundError:
        return False, "`brew` not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"`brew services restart {formula}` hung >{_RESTART_TIMEOUT_S:.0f}s"
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"`brew services restart {formula}` failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, (f"`brew services restart {formula}` exited "
                       f"{proc.returncode}: {tail[-1] if tail else '(no output)'}")
    return True, "brew services restart ok"


def _wait_healthy(health, wait_s: float) -> float | None:
    """Poll `health()` until true; returns seconds taken, or None."""
    t0 = _now()
    deadline = t0 + wait_s
    while _now() < deadline:
        try:
            if health():
                return _now() - t0
        except Exception:
            pass
        time.sleep(1.0)
    return None


def repair_omlx(*, base_url: str, health, error_text: str = "",
                engine: str = "omlx", force: bool = False,
                check: StaleCheck | None = None,
                wait_s: float = _HEALTH_WAIT_S) -> RepairResult:
    """Restart a stale local oMLX and wait for it — or explain the refusal.

    `health` is a zero-arg callable (the Backend's `health`). `error_text`
    is the failure body that prompted the call, when there is one.
    `force` skips the signature test (the explicit `luxe repair --force`),
    never the locality / brew / cooldown guards.
    """
    global _last_restart_at
    try:
        return _repair_omlx(base_url=base_url, health=health,
                            error_text=error_text, engine=engine,
                            force=force, check=check, wait_s=wait_s)
    except Exception as e:  # noqa: BLE001 - see module docstring
        return RepairResult(reason=f"repair errored: {e}")


def _repair_omlx(*, base_url, health, error_text, engine, force, check,
                 wait_s) -> RepairResult:
    global _last_restart_at
    from luxe.chat.origin import endpoint_is_local

    res = RepairResult()
    if engine != "omlx":
        res.reason = f"engine {engine!r} is not brew-managed oMLX"
        return res
    if not endpoint_is_local(base_url):
        res.reason = f"{base_url} is not on this machine — its host must restart it"
        return res
    if not _installed_versions("omlx"):
        res.reason = "omlx is not brew-installed here (nothing to `brew services restart`)"
        return res
    left = _in_cooldown()
    if left > 0:
        res.reason = (f"oMLX was already restarted {COOLDOWN_S - left:.0f}s ago and "
                      "is still failing — that is a different problem; see "
                      "`brew services info omlx` / `tail ~/.omlx/omlx.log`")
        return res

    check = check if check is not None else check_omlx()
    proc_stale = check.conclusive and check.stale
    text_stale = looks_stale(error_text)
    if not (proc_stale or text_stale or force):
        res.reason = ("not the stale-oMLX signature (process matches the "
                      "installed build and the error names no missing module)")
        return res
    why = []
    if proc_stale:
        why.append(check.detail)
    if text_stale:
        why.append("load failure names a module the running tree no longer has")
    if force and not why:
        why.append("forced")
    res.reason = "; ".join(why)
    res.attempted = True
    _last_restart_at = _now()
    t0 = _now()

    res.steps.append("brew services restart omlx")
    ok, msg = _restart_service("omlx")
    res.steps.append(msg)
    if not ok:
        res.seconds = _now() - t0
        return res

    took = _wait_healthy(health, wait_s)
    if took is None:
        res.steps.append(f"{base_url} not healthy after {wait_s:.0f}s")
        res.seconds = _now() - t0
        return res
    res.steps.append(f"endpoint healthy after {took:.0f}s")

    after = check_omlx()
    if after.conclusive and after.stale:
        res.steps.append(f"still stale: {after.detail}")
        res.seconds = _now() - t0
        return res
    if after.conclusive:
        res.steps.append(f"build {after.running} (matches installed)")
    res.ok = True
    res.seconds = _now() - t0
    return res
