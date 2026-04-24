"""Bash tool with an allowlist. Runs inside the repo root."""

from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any

from harness.backends import ToolDef

from luxe.tools import fs

DEFAULT_ALLOWLIST = (
    "cargo", "pytest", "go", "python", "python3", "rustc",
    "node", "npm", "pnpm", "yarn",
    "git", "ls", "pwd", "cat", "head", "tail", "echo", "wc",
    # Static-analysis binaries. The dedicated `lint`/`typecheck`/etc.
    # tools call these with narrow flags, but `bash` is the escape
    # hatch for unusual invocations (e.g. `ruff format`, `ruff
    # --fix`, specific bandit plugins, a custom mypy config).
    "ruff", "mypy", "bandit", "pip-audit", "semgrep", "gitleaks",
    # Cross-language analyzers. Same pattern — dedicated tool fns
    # call these with narrow flags; `bash` is the escape hatch for
    # non-standard invocations (`cargo clippy --fix`, `tsc --watch`,
    # `eslint --fix`).
    "eslint", "tsc", "clippy", "staticcheck",
)

_ALLOWLIST: tuple[str, ...] = DEFAULT_ALLOWLIST


def set_allowlist(allowlist: tuple[str, ...]) -> None:
    global _ALLOWLIST
    _ALLOWLIST = allowlist


def tool_defs() -> list[ToolDef]:
    allowed = ", ".join(_ALLOWLIST)
    return [
        ToolDef(
            name="bash",
            description=(
                "Run an allowlisted shell command inside the repo root. "
                f"Allowed leading binaries: {allowed}. Output is truncated "
                "to 8 KB per stdout/stderr."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "timeout_s": {"type": "integer", "default": 120, "minimum": 1, "maximum": 600},
                },
                "required": ["cmd"],
            },
        )
    ]


def bash(args: dict[str, Any]) -> tuple[Any, str | None]:
    cmd = args["cmd"]
    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        return None, f"unparseable command: {e}"
    if not parts or parts[0] not in _ALLOWLIST:
        return None, (
            f"command '{parts[0] if parts else ''}' not in allowlist {_ALLOWLIST}"
        )
    try:
        res = subprocess.run(  # noqa: S603
            parts,
            cwd=fs.repo_root(),
            capture_output=True,
            text=True,
            timeout=int(args.get("timeout_s", 120)),
        )
    except subprocess.TimeoutExpired:
        return None, f"command timed out after {args.get('timeout_s', 120)}s"

    return (
        json.dumps(
            {
                "exit_code": res.returncode,
                "stdout": _trim(res.stdout, 8000),
                "stderr": _trim(res.stderr, 8000),
            }
        ),
        None,
    )


def _trim(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit] + f"\n... [truncated {len(s) - limit} bytes]"


TOOL_FNS = {"bash": bash}
