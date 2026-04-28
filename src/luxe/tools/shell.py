"""Allowlisted shell execution — scoped to repo root."""

from __future__ import annotations

import subprocess
from typing import Any

from luxe.tools.base import ToolDef, ToolFn
from luxe.tools.fs import _REPO_ROOT

_ALLOWLIST = frozenset({
    "cargo", "cat", "echo", "find", "git", "go", "grep", "head",
    "ls", "make", "npm", "npx", "pip", "pytest", "python", "ruff",
    "sed", "sort", "tail", "tree", "wc",
})

_MAX_OUTPUT = 8192
_TIMEOUT = 60


def _bash(args: dict[str, Any]) -> tuple[str, str | None]:
    if _REPO_ROOT is None:
        return "", "Repo root not set"

    command = args["command"]
    parts = command.strip().split()
    if not parts:
        return "", "Empty command"

    binary = parts[0]
    if binary not in _ALLOWLIST:
        return "", f"Command '{binary}' not in allowlist. Allowed: {sorted(_ALLOWLIST)}"

    try:
        proc = subprocess.run(
            command, shell=True,
            capture_output=True, text=True,
            cwd=_REPO_ROOT, timeout=_TIMEOUT,
        )
        output = proc.stdout + proc.stderr
        if len(output) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + f"\n... (truncated at {_MAX_OUTPUT} bytes)"
        return output, None if proc.returncode == 0 else f"exit code {proc.returncode}"
    except subprocess.TimeoutExpired:
        return "", f"Command timed out after {_TIMEOUT}s"


def tool_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name="bash",
            description=f"Run a shell command (allowlisted binaries: {', '.join(sorted(_ALLOWLIST))}). Scoped to repo root.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                },
                "required": ["command"],
            },
        ),
    ]


TOOL_FNS: dict[str, ToolFn] = {
    "bash": _bash,
}
