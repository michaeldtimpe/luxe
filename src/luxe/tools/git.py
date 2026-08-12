"""Git inspection tools — read-only."""

from __future__ import annotations

import subprocess
from typing import Any

from luxe import gitcmd
from luxe.tools.base import ToolDef, ToolFn
from luxe.tools.fs import get_repo_root


#: Ends git's option parsing: everything after it is a revision or a path, even
#: when it looks like a flag. Model-supplied `ref` values go behind this —
#: without it `git_diff(ref="--output=/tmp/x.patch")` wrote a patch file OUTSIDE
#: the repo from a READ-ONLY tool and returned ("", None), i.e. a write nobody
#: asked for reported as an empty diff. `--` still separates revisions from
#: paths after it (git ≥ 2.24).
_EOO = "--end-of-options"


def _run_git(*cmd: str, max_output: int = 32768) -> tuple[str, str | None]:
    repo_root = get_repo_root()
    if repo_root is None:
        return "", "Repo root not set"
    try:
        proc = gitcmd.run_in(repo_root, *cmd, timeout=30)
        if proc.returncode != 0:
            return "", proc.stderr.strip() or f"git exited with {proc.returncode}"
        return _truncated(proc.stdout, max_output), None
    except FileNotFoundError:
        return "", "git not found on PATH"
    except subprocess.TimeoutExpired:
        return "", "git command timed out"


def _truncated(out: str, max_output: int) -> str:
    """`out`, cut at `max_output` bytes WITH the cut announced.

    The slice used to be silent and mid-line: `git_diff(ref=HEAD~40)` returned
    exactly 32,768 characters ending in the middle of a hunk, with err=None —
    a partial diff that reads as the whole diff, and a final record the model
    cannot parse. Same rule as grep and read_file: state the cut and the way
    around it. Output under the cap is byte-identical.
    """
    if len(out) <= max_output:
        return out
    body = out[:max_output]
    body = body[:body.rfind("\n") + 1] or body   # don't end mid-line
    return body + (f"[truncated at {max_output:,} bytes — narrow with a path "
                   f"argument or a smaller ref range]\n")


def _git_diff(args: dict[str, Any]) -> tuple[str, str | None]:
    cmd = ["diff"]
    if args.get("staged"):
        cmd.append("--staged")
    if args.get("ref"):
        cmd.extend([_EOO, args["ref"]])
    if args.get("path"):
        cmd.extend(["--", args["path"]])
    return _run_git(*cmd)


def _git_log(args: dict[str, Any]) -> tuple[str, str | None]:
    try:
        # `-{n}` is spliced into argv, so a non-integer n is an option-shaped
        # value in disguise (n="-p --output=x" would be four more flags).
        n = int(args.get("n", 20))
    except (TypeError, ValueError):
        return "", f"git_log: n must be an integer, got {args.get('n')!r}"
    cmd = ["log", f"-{n}", "--oneline", "--no-decorate"]
    if args.get("path"):
        cmd.extend(["--", args["path"]])
    return _run_git(*cmd)


def _git_show(args: dict[str, Any]) -> tuple[str, str | None]:
    ref = args.get("ref", "HEAD")
    # Options first, then the ref behind _EOO — `git show <opts> <ref>` and
    # `git show <ref> <opts>` produce identical output.
    cmd = ["show", "--stat", "--format=commit %H%nAuthor: %an%nDate: %ad%n%n%s%n%b",
           _EOO, ref]
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
