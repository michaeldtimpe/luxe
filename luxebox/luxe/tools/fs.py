"""Filesystem tools scoped to a repo root.

Adapted from personal_eval/agent_loop.py — same _safe_path boundary guard,
expanded toolbox (edit_file, glob, list_dir). All paths are repo-relative.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any

from harness.backends import ToolDef

from luxe.tools._subprocess import run_binary

_REPO_ROOT: Path = Path.cwd()
MAX_FILE_BYTES = 256 * 1024
MAX_GREP_MATCHES = 150
MAX_GLOB_MATCHES = 500


def set_repo_root(path: str | Path) -> None:
    global _REPO_ROOT
    _REPO_ROOT = Path(str(path)).expanduser().resolve()


def repo_root() -> Path:
    return _REPO_ROOT


def _safe(rel: str) -> Path:
    p = (_REPO_ROOT / rel).resolve()
    if _REPO_ROOT not in p.parents and p != _REPO_ROOT:
        raise PermissionError(f"path escapes repo root: {rel}")
    return p


def read_only_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name="read_file",
            description="Read a UTF-8 text file. Path is relative to repo root.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        ToolDef(
            name="list_dir",
            description="List entries in a directory (default: repo root).",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "required": [],
            },
        ),
        ToolDef(
            name="glob",
            description=(
                "Find files matching a glob pattern (e.g. 'src/**/*.py'). "
                "Returns at most 500 matching paths."
            ),
            parameters={
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        ),
        ToolDef(
            name="grep",
            description=(
                "Ripgrep over the repo. Returns up to 150 matching lines "
                "with file:line:text. `glob` optionally filters to a subset."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string"},
                },
                "required": ["pattern"],
            },
        ),
    ]


def mutation_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name="write_file",
            description="Write UTF-8 text to a file (creates or overwrites).",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        ),
        ToolDef(
            name="edit_file",
            description=(
                "Targeted string replace in a file. `old_string` must match "
                "exactly and appear exactly once; use a longer context "
                "window if it isn't unique. Safer than write_file for edits."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        ),
    ]


def read_file(args: dict[str, Any]) -> tuple[Any, str | None]:
    p = _safe(args["path"])
    data = p.read_bytes()
    truncated = len(data) > MAX_FILE_BYTES
    if truncated:
        data = data[:MAX_FILE_BYTES]
    text = data.decode("utf-8", errors="replace")
    if truncated:
        text += f"\n... [truncated at {MAX_FILE_BYTES} bytes]"
    return text, None


def list_dir(args: dict[str, Any]) -> tuple[Any, str | None]:
    p = _safe(args.get("path", "."))
    entries = sorted(
        entry.name + ("/" if entry.is_dir() else "") for entry in p.iterdir()
    )
    return json.dumps(entries), None


def glob_files(args: dict[str, Any]) -> tuple[Any, str | None]:
    pattern = args["pattern"]
    matches: list[str] = []
    for p in _REPO_ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(_REPO_ROOT).as_posix()
        if fnmatch.fnmatch(rel, pattern):
            matches.append(rel)
            if len(matches) >= MAX_GLOB_MATCHES:
                break
    return json.dumps(matches), None


def grep(args: dict[str, Any]) -> tuple[Any, str | None]:
    pattern = args["pattern"]
    rg_args = ["rg", "--no-heading", "--color=never", "-n", pattern]
    if args.get("glob"):
        rg_args += ["-g", args["glob"]]
    out, err = run_binary(
        rg_args,
        cwd=_REPO_ROOT,
        timeout_s=30,
        missing_hint="brew install ripgrep",
        allow_nonzero_exit=True,  # rg exits 1 on no matches — not an error
    )
    if err:
        return None, err
    lines = (out or "").splitlines()
    return "\n".join(lines[:MAX_GREP_MATCHES]), None


def write_file(args: dict[str, Any]) -> tuple[Any, str | None]:
    p = _safe(args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    content = args["content"]
    p.write_text(content)
    return f"wrote {len(content)} bytes to {args['path']}", None


def edit_file(args: dict[str, Any]) -> tuple[Any, str | None]:
    p = _safe(args["path"])
    old, new = args["old_string"], args["new_string"]
    text = p.read_text()
    occurrences = text.count(old)
    if occurrences == 0:
        return None, "old_string not found"
    if occurrences > 1:
        return None, f"old_string appears {occurrences} times — make it unique"
    p.write_text(text.replace(old, new, 1))
    return f"edited {args['path']} (1 replacement)", None


READ_ONLY_FNS = {
    "read_file": read_file,
    "list_dir": list_dir,
    "glob": glob_files,
    "grep": grep,
}

MUTATION_FNS = {
    "write_file": write_file,
    "edit_file": edit_file,
}
