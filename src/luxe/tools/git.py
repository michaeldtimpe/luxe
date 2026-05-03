"""Git inspection tools — read-only."""

from __future__ import annotations

import subprocess
from typing import Any

from luxe.tools.base import ToolDef, ToolFn
from luxe.tools.fs import get_repo_root


def _run_git(*cmd: str, max_output: int = 32768) -> tuple[str, str | None]:
    repo_root = get_repo_root()
    if repo_root is None:
        return "", "Repo root not set"
    try:
        proc = subprocess.run(
            ["git", *cmd],
            capture_output=True, text=True,
            cwd=repo_root, timeout=30,
        )
        if proc.returncode != 0:
            return "", proc.stderr.strip() or f"git exited with {proc.returncode}"
        return proc.stdout[:max_output], None
    except FileNotFoundError:
        return "", "git not found on PATH"
    except subprocess.TimeoutExpired:
        return "", "git command timed out"


def _git_diff(args: dict[str, Any]) -> tuple[str, str | None]:
    cmd = ["diff"]
    if args.get("staged"):
        cmd.append("--staged")
    if args.get("ref"):
        cmd.append(args["ref"])
    if args.get("path"):
        cmd.extend(["--", args["path"]])
    return _run_git(*cmd)


def _git_log(args: dict[str, Any]) -> tuple[str, str | None]:
    n = args.get("n", 20)
    cmd = ["log", f"-{n}", "--oneline", "--no-decorate"]
    if args.get("path"):
        cmd.extend(["--", args["path"]])
    return _run_git(*cmd)


def _git_show(args: dict[str, Any]) -> tuple[str, str | None]:
    ref = args.get("ref", "HEAD")
    cmd = ["show", ref, "--stat", "--format=commit %H%nAuthor: %an%nDate: %ad%n%n%s%n%b"]
    return _run_git(*cmd)


def tool_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name="git_diff",
            description="Show git diff. Optionally filter by path or ref, or show staged changes.",
            parameters={
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean", "description": "Show staged changes only"},
                    "ref": {"type": "string", "description": "Diff against this ref (branch/commit)"},
                    "path": {"type": "string", "description": "Limit diff to this path"},
                },
                "required": [],
            },
        ),
        ToolDef(
            name="git_log",
            description="Show recent git commits (oneline format).",
            parameters={
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "Number of commits (default 20)"},
                    "path": {"type": "string", "description": "Limit to commits touching this path"},
                },
                "required": [],
            },
        ),
        ToolDef(
            name="git_show",
            description="Show details of a specific commit.",
            parameters={
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Commit hash or ref (default HEAD)"},
                },
                "required": [],
            },
        ),
    ]


TOOL_FNS: dict[str, ToolFn] = {
    "git_diff": _git_diff,
    "git_log": _git_log,
    "git_show": _git_show,
}

CACHEABLE = {"git_log", "git_show"}
