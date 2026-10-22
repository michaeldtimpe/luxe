"""The `LUXE_*` switches the golden snapshots clear before they run.

Both golden modules (`test_golden_request.py`, `test_golden_trajectory.py`)
describe the substrate under an exact environment, so anything the
operator's shell exports must be cleared first. A hand-kept list rotted: it
went on clearing switches that had been removed and missed ones added since.

`RunFlags.from_env` is the one place `run_agent` reads its switches
(agents.sdd), so that set is DERIVED by recording which names it asks for —
a new switch is covered the day it lands. The few reads that live outside
RunFlags but are reachable from `run_single` / `run_agent` / `Backend` are
listed by hand below.
"""

from __future__ import annotations

from luxe.agents.flags import RunFlags


class _RecordingEnv(dict):
    """An empty environment that remembers every name looked up in it."""

    def __init__(self) -> None:
        super().__init__()
        self.read: list[str] = []

    def get(self, key, default=None):  # type: ignore[override]
        self.read.append(key)
        return default


def run_flags_switches() -> tuple[str, ...]:
    """Every variable `RunFlags.from_env` reads, in read order."""
    env = _RecordingEnv()
    RunFlags.from_env(env)
    return tuple(dict.fromkeys(env.read))


# Live reads outside RunFlags, cleared defensively: Backend's payload-suspect
# retry (on this path), the ctx-scaled read budget (maintain/chat set it), the
# reflect gate (BFCL's verify stage), and the token log interval (bound at
# loop.py import, so clearing it is belt-and-braces).
OTHER_SWITCHES = (
    "LUXE_REFLECT",
    "LUXE_TOOL_BUDGET_CTX",
    "LUXE_PAYLOAD_SUSPECT_RETRY",
    "LUXE_TOKEN_LOG_INTERVAL",
)

GOLDEN_SWITCHES: tuple[str, ...] = run_flags_switches() + OTHER_SWITCHES
