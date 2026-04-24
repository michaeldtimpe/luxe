"""Scoped git tools for review/refactor agents.

The review and refactor agents are read-only (no bash), so they can't
reach the git history that explains *why* code looks the way it does.
Without that, findings tend to restate the current snapshot rather
than flag drift. These three tools expose just enough of git to ground
review in recent change context.

All commands run with cwd set to `fs.repo_root()` so paths stay scoped.
"""

from __future__ import annotations

from typing import Any

from harness.backends import ToolDef

from cli.tools import fs
from cli.tools._subprocess import run_binary

MAX_OUTPUT_CHARS = 16000


def tool_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name="git_diff",
            description=(
                "Show unstaged + staged changes, or a diff against a ref. "
                "Default `ref=HEAD` diffs the working tree against the last "
                "commit. Pass `ref='main..HEAD'` etc. for branch ranges, or "
                "`path` to narrow to a file/subtree."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "default": "HEAD"},
                    "path": {"type": "string"},
                },
                "required": [],
            },
        ),
        ToolDef(
            name="git_log",
            description=(
                "Recent commit history (one line per commit: short sha + "
                "author date + subject). Defaults to 20 commits on the "
                "current branch."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
                    "path": {"type": "string"},
                },
                "required": [],
            },
        ),
        ToolDef(
            name="git_show",
            description=(
                "Show a single commit's full patch by short or full sha."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sha": {"type": "string"},
                },
                "required": ["sha"],
            },
        ),
    ]


def _run(cmd: list[str]) -> tuple[str, str | None]:
    out, err = run_binary(
        cmd,
        cwd=fs.repo_root(),
        timeout_s=30,
        max_output_chars=MAX_OUTPUT_CHARS,
        missing_hint="git not installed on PATH",
    )
    if err:
        return "", err
    return out or "", None


def git_diff(args: dict[str, Any]) -> tuple[Any, str | None]:
    ref = (args.get("ref") or "HEAD").strip()
    path = (args.get("path") or "").strip()
    cmd = ["git", "diff", ref]
    if path:
        cmd += ["--", path]
    return _run(cmd)


def git_log(args: dict[str, Any]) -> tuple[Any, str | None]:
    limit = max(1, min(int(args.get("limit") or 20), 200))
    path = (args.get("path") or "").strip()
    cmd = [
        "git", "log", f"-{limit}",
        "--pretty=format:%h %ad %an — %s", "--date=short",
    ]
    if path:
        cmd += ["--", path]
    return _run(cmd)


def git_show(args: dict[str, Any]) -> tuple[Any, str | None]:
    sha = (args.get("sha") or "").strip()
    if not sha:
        return None, "sha required"
    return _run(["git", "show", sha])


TOOL_FNS = {
    "git_diff": git_diff,
    "git_log": git_log,
    "git_show": git_show,
}
