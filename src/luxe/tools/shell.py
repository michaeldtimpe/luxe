"""Allowlisted shell execution — scoped to repo root.

Hardened 2026-05-02 against allowlist-bypass via shell chains. The
original implementation checked `parts[0]` against the allowlist
and ran `command` via `shell=True`, which meant a command like
`cat foo && rm -rf /` passed the check (parts[0] == 'cat') and
then the shell still executed `rm`. The fix uses `shlex.split` to
tokenize the command, then rejects any chain operator (`&&`, `||`,
`;`, `|`, `&`) or redirect (`>`, `<`, `>>`) or command-substitution
(`` ` ``, `$(`) tokens that would bypass the allowlist. The model
can issue multiple bash calls if it needs sequential commands;
write_file/read_file are the right tools for what redirects would
otherwise do.

Glob expansion and other shell features still work — `shell=True`
is preserved for the single, allowlisted binary.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Any

from luxe.tools.base import ToolDef, ToolFn
from luxe.tools.fs import get_repo_root

_ALLOWLIST = frozenset({
    "cargo", "cat", "echo", "find", "git", "go", "grep", "head",
    "ls", "make", "npm", "npx", "pip", "pytest", "python", "ruff",
    "sed", "sort", "tail", "tree", "wc",
})

# Tokens that would cause the shell to run additional binaries beyond
# the one we allowlisted. Detected as standalone tokens after shlex.split,
# so quoted arguments containing these characters (e.g. a regex pattern)
# don't trip the check.
_CHAIN_TOKENS = frozenset({"&&", "||", ";", "|", "&"})
_REDIRECT_TOKENS = frozenset({">", "<", ">>", "<<", "<<<", "&>", "2>", "2>&1"})

_MAX_OUTPUT = 8192
_TIMEOUT = 60


def _validate_command(command: str) -> tuple[list[str], str | None]:
    """Tokenize via shlex and reject anything that would let a non-allowlisted
    binary execute. Returns (tokens, error). On error, tokens is empty.

    shlex.split respects quoting so `grep "a|b" file` is 3 tokens
    (`grep`, `a|b`, `file`) — not 5 — and the literal `|` inside the
    quoted regex doesn't trip the chain check.
    """
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        return [], f"Failed to parse command (mismatched quotes?): {e}"
    if not tokens:
        return [], "Empty command"

    bad_chain = [t for t in tokens if t in _CHAIN_TOKENS or t in _REDIRECT_TOKENS]
    if bad_chain:
        return [], (
            f"Shell chain/redirect operators not allowed: {bad_chain}. "
            "Issue separate bash calls for each command, or use "
            "read_file/write_file/edit_file instead of redirects."
        )

    # Command substitution — `cat $(echo /etc/passwd)` would let any binary
    # execute via the inner shell. Reject backticks and $(.
    for t in tokens:
        if "`" in t or "$(" in t:
            return [], (
                f"Command substitution not allowed in token {t!r}. "
                "Inner commands bypass the bash allowlist."
            )

    return tokens, None


def _bash(args: dict[str, Any]) -> tuple[str, str | None]:
    repo_root = get_repo_root()
    if repo_root is None:
        return "", "Repo root not set"

    command = args["command"]
    tokens, err = _validate_command(command)
    if err:
        return "", err

    binary = tokens[0]
    if binary not in _ALLOWLIST:
        return "", f"Command '{binary}' not in allowlist. Allowed: {sorted(_ALLOWLIST)}"

    try:
        proc = subprocess.run(
            command, shell=True,
            capture_output=True, text=True,
            cwd=repo_root, timeout=_TIMEOUT,
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
