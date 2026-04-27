"""Startup venv mismatch warning.

If the user has a venv active ($VIRTUAL_ENV) that's different from the
one luxe is actually running in (sys.prefix), check whether the user's
venv has luxe's runtime dependencies importable. If not, print one
short warning with a concrete install command — the user often wants
to keep tooling consistent across projects but can't tell at a glance
whether their venv is missing anything.

Silenced by LUXE_NO_VENV_CHECK=1 (any non-empty value works).

The check spawns the user's interpreter once with a short -c probe.
That's a ~50 ms one-shot at startup; it does NOT re-exec luxe in the
user's venv (intentional — see the conversation in PR #2: their venv
might have an older luxe_cli installed and silently take over).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


# Modules luxe imports unconditionally at runtime. Keep in sync with
# pyproject.toml [project].dependencies. Optional deps (mlx, browser)
# stay out of this check — most installs don't use them.
_REQUIRED_MODULES: tuple[str, ...] = (
    "httpx",
    "pydantic",
    "yaml",
    "rich",
    "typer",
    "psutil",
    "jinja2",
    "tenacity",
    "tqdm",
    "prompt_toolkit",
)


def _user_venv_python() -> Path | None:
    """Path to the active venv's python, if VIRTUAL_ENV is set and != ours."""
    venv = os.environ.get("VIRTUAL_ENV", "").strip()
    if not venv:
        return None
    venv_path = Path(venv).resolve()
    # If the user's active venv IS the one we're running in, no mismatch.
    if venv_path == Path(sys.prefix).resolve():
        return None
    # macOS / Linux convention.
    candidate = venv_path / "bin" / "python"
    if candidate.exists():
        return candidate
    # Windows fallback (uncommon for luxe but harmless to try).
    candidate = venv_path / "Scripts" / "python.exe"
    if candidate.exists():
        return candidate
    return None


def _missing_modules(python_bin: Path) -> list[str]:
    """Return the subset of _REQUIRED_MODULES that the given python can't import."""
    probe = (
        "import sys\n"
        "missing = []\n"
        f"for m in {list(_REQUIRED_MODULES)!r}:\n"
        "    try:\n"
        "        __import__(m)\n"
        "    except ImportError:\n"
        "        missing.append(m)\n"
        "print('\\n'.join(missing))\n"
    )
    try:
        r = subprocess.run(
            [str(python_bin), "-c", probe],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []  # probe failed — don't warn on a flaky check
    if r.returncode != 0:
        return []
    return [m for m in r.stdout.strip().splitlines() if m]


def warn_if_venv_mismatch() -> str | None:
    """Build a one-line warning if the active venv lacks luxe deps.

    Returns the warning string for the caller to print, or None when
    nothing's wrong / the check is silenced. Returning a string instead
    of printing keeps this trivially testable.
    """
    if os.environ.get("LUXE_NO_VENV_CHECK", "").strip():
        return None
    user_python = _user_venv_python()
    if user_python is None:
        return None
    missing = _missing_modules(user_python)
    if not missing:
        # Their venv has every dep — luxe could run there cleanly. No
        # need to warn; subprocess tools (lint, typecheck) using PATH
        # already pick up the user's venv binaries.
        return None
    luxe_root = Path(__file__).resolve().parent.parent  # luxe/luxe_cli/.. → luxe/
    return (
        f"active venv ({os.environ['VIRTUAL_ENV']}) is missing: "
        f"{', '.join(missing)}. "
        f"To unify: cd {luxe_root} && uv pip install -e . "
        f"(silence: export LUXE_NO_VENV_CHECK=1)"
    )
