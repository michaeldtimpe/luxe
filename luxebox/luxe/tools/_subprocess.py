"""Shared subprocess runner for tool wrappers.

Multiple tool modules shell out to external binaries — git, ripgrep,
ruff, mypy, bandit, pip-audit, semgrep, gitleaks. They all want the
same shape: run the command, cap the output, surface missing binaries
with a helpful hint, and return the `(result, err)` tuple the agent
loop expects. Unifying it keeps each new analyzer tool to ~10 lines.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _resolve_binary(name: str) -> str | None:
    """Find a binary, preferring the current venv's `bin/` before
    falling back to system `PATH`. Returns None if not found anywhere.

    Analyzers like ruff/mypy/bandit/pip-audit are installed via uv sync
    into `.venv/bin/`, which isn't on the shell's PATH when luxe runs
    as a daemon. Ripgrep and git are typically on system PATH from
    Homebrew. Checking the venv first covers both cases uniformly."""
    if "/" in name or "\\" in name:  # already an absolute/explicit path
        return name if Path(name).exists() else None
    venv_bin = Path(sys.executable).parent / name
    if venv_bin.exists() and venv_bin.is_file():
        return str(venv_bin)
    return shutil.which(name)


def run_binary(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout_s: int = 30,
    max_output_chars: int = 16_000,
    missing_hint: str = "",
    allow_nonzero_exit: bool = False,
) -> tuple[str | None, str | None]:
    """Run an external binary and return `(stdout_trimmed, err)`.

    - Missing binary → `(None, "<argv0> not installed. <hint>")`.
    - Timeout → `(None, "<argv0> timed out after Ns")`.
    - Non-zero exit, `allow_nonzero_exit=False` → `("", stderr_or_code)`.
    - Non-zero exit, `allow_nonzero_exit=True` → `(stdout_trimmed, None)`.
      Useful for tools where a non-zero code signals findings
      (`rg` → 1 on no matches, `ruff`/`mypy`/`bandit` → 1 when
      diagnostics present).
    - Exit 0 → `(stdout_trimmed, None)`.
    """
    binary = _resolve_binary(argv[0])
    if binary is None:
        hint = f" {missing_hint}" if missing_hint else ""
        return None, f"{argv[0]} not installed.{hint}"
    resolved_argv = [binary, *argv[1:]]
    try:
        res = subprocess.run(  # noqa: S603
            resolved_argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return None, f"{argv[0]} timed out after {timeout_s}s"

    if res.returncode != 0 and not allow_nonzero_exit:
        err = (res.stderr or "").strip() or f"{argv[0]} exited {res.returncode}"
        return "", err

    out = res.stdout or ""
    if len(out) > max_output_chars:
        out = out[:max_output_chars] + f"\n... [truncated at {max_output_chars} bytes]"
    return out, None
