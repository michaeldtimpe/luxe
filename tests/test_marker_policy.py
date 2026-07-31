"""The `live_*` markers must actually deselect by default.

`pyproject.toml` registers `live_model` and `live_backend` as "skip by
default", and `tests/test_mlx_direct_smoke.py`'s docstring repeats the
promise. For a long time nothing implemented it: the marked tests ran on
every invocation, loading real MLX weights (~17% of the suite's wall) and
turning into hard `ModuleNotFoundError` errors on any machine without
Apple-silicon MLX — which is every Linux CI runner.

These tests pin the policy so the promise can't silently lapse again.
They shell out to a subprocess because marker selection is resolved during
collection, before the in-process test session can observe it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
LIVE_MODULE = "tests/test_mlx_direct_smoke.py"


def _collect(*extra: str) -> str:
    """Run `pytest --collect-only` in a subprocess and return its output."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", LIVE_MODULE, "--collect-only", "-q", *extra],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
    )
    return proc.stdout + proc.stderr


def test_live_model_is_deselected_by_default():
    """A bare pytest run must not collect the live-model module."""
    out = _collect()
    assert "test_token_logprobs_basic" not in out, (
        "live_model tests are being collected by default — the addopts marker "
        "filter in pyproject.toml is missing or no longer takes effect.\n" + out
    )
    assert "4 deselected" in out or "no tests ran" in out or "4/4 deselected" in out, out


def test_live_model_is_reselectable_on_demand():
    """The documented manual invocation must still work.

    `-m live_model` on the command line has to beat the `-m` in addopts,
    otherwise the marker filter would make these tests unrunnable rather
    than opt-in.
    """
    out = _collect("-m", "live_model")
    assert "test_token_logprobs_basic" in out, (
        "-m live_model no longer re-selects the live tests; the addopts filter "
        "has made them unreachable instead of opt-in.\n" + out
    )
    assert "4 tests collected" in out, out


def test_markers_are_registered():
    """Both live markers stay declared, so `--strict-markers` users are safe."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert "live_model:" in text
    assert "live_backend:" in text
