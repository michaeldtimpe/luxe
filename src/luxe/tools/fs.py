"""Filesystem tools — scoped to repo root for safety."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from luxe.tools.base import ToolDef, ToolFn

_REPO_ROOT: Path | None = None
_MAX_FILE_SIZE = 256 * 1024  # 256 KB read limit
_MAX_RESULTS = 150


def set_repo_root(path: str | Path) -> None:
    global _REPO_ROOT
    _REPO_ROOT = Path(path).resolve()


def _safe(rel: str) -> Path:
    if _REPO_ROOT is None:
        raise RuntimeError("Repo root not set — call set_repo_root() first")
    resolved = (_REPO_ROOT / rel).resolve()
    if not str(resolved).startswith(str(_REPO_ROOT)):
        raise PermissionError(f"Path escapes repo root: {rel}")
    return resolved


def _read_file(args: dict[str, Any]) -> tuple[str, str | None]:
    path = _safe(args["path"])
    if not path.is_file():
        return "", f"File not found: {args['path']}"
    size = path.stat().st_size
    if size > _MAX_FILE_SIZE:
        return "", f"File too large ({size} bytes, limit {_MAX_FILE_SIZE})"
    try:
        text = path.read_text(errors="replace")
    except Exception as e:
        return "", str(e)
    offset = args.get("offset", 0)
    limit = args.get("limit")
    lines = text.splitlines(keepends=True)
    if offset:
        lines = lines[offset:]
    if limit:
        lines = lines[:limit]
    numbered = [f"{i + offset + 1}\t{line}" for i, line in enumerate(lines)]
    return "".join(numbered), None


def _list_dir(args: dict[str, Any]) -> tuple[str, str | None]:
    path = _safe(args.get("path", "."))
    if not path.is_dir():
        return "", f"Not a directory: {args.get('path', '.')}"
    entries = sorted(path.iterdir())
    lines = []
    for e in entries[:_MAX_RESULTS]:
        suffix = "/" if e.is_dir() else ""
        lines.append(f"{e.name}{suffix}")
    result = "\n".join(lines)
    if len(entries) > _MAX_RESULTS:
        result += f"\n... ({len(entries) - _MAX_RESULTS} more)"
    return result, None


def _glob(args: dict[str, Any]) -> tuple[str, str | None]:
    if _REPO_ROOT is None:
        return "", "Repo root not set"
    pattern = args["pattern"]
    matches = sorted(_REPO_ROOT.glob(pattern))
    lines = [str(m.relative_to(_REPO_ROOT)) for m in matches[:_MAX_RESULTS]]
    result = "\n".join(lines)
    if len(matches) > _MAX_RESULTS:
        result += f"\n... ({len(matches) - _MAX_RESULTS} more)"
    return result, None


def _grep(args: dict[str, Any]) -> tuple[str, str | None]:
    if _REPO_ROOT is None:
        return "", "Repo root not set"
    pattern = args["pattern"]
    file_glob = args.get("glob", "")
    try:
        cmd = ["rg", "--no-heading", "-n", "--max-count=150", pattern]
        if file_glob:
            cmd.extend(["--glob", file_glob])
        proc = subprocess.run(
            cmd, capture_output=True, text=True, cwd=_REPO_ROOT, timeout=30,
        )
        return proc.stdout[:32768] if proc.stdout else "(no matches)", None
    except FileNotFoundError:
        lines = []
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return "", f"Invalid pattern: {e}"
        for root, _, files in os.walk(_REPO_ROOT):
            for f in files:
                if file_glob and not fnmatch.fnmatch(f, file_glob):
                    continue
                fp = Path(root) / f
                try:
                    for i, line in enumerate(fp.open(errors="replace"), 1):
                        if regex.search(line):
                            rel = fp.relative_to(_REPO_ROOT)
                            lines.append(f"{rel}:{i}:{line.rstrip()}")
                            if len(lines) >= _MAX_RESULTS:
                                return "\n".join(lines), None
                except (OSError, UnicodeDecodeError):
                    continue
        return "\n".join(lines) if lines else "(no matches)", None


def _write_file(args: dict[str, Any]) -> tuple[str, str | None]:
    path = _safe(args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(args["content"])
    except Exception as e:
        return "", str(e)
    return f"Wrote {len(args['content'])} bytes to {args['path']}", None


def _edit_file(args: dict[str, Any]) -> tuple[str, str | None]:
    path = _safe(args["path"])
    if not path.is_file():
        return "", f"File not found: {args['path']}"
    try:
        text = path.read_text()
    except Exception as e:
        return "", str(e)
    old = args["old_string"]
    new = args["new_string"]
    count = text.count(old)
    if count == 0:
        return "", f"old_string not found in {args['path']}"
    if count > 1 and not args.get("replace_all", False):
        return "", f"old_string matches {count} times — use replace_all or provide more context"
    text = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
    path.write_text(text)
    return f"Edited {args['path']} ({count} replacement{'s' if count > 1 else ''})", None


def read_only_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name="read_file",
            description="Read a file's contents with line numbers. Use offset/limit for large files.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from repo root"},
                    "offset": {"type": "integer", "description": "Start line (0-based)"},
                    "limit": {"type": "integer", "description": "Max lines to return"},
                },
                "required": ["path"],
            },
        ),
        ToolDef(
            name="list_dir",
            description="List directory contents. Directories end with /.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path (default: repo root)"},
                },
                "required": [],
            },
        ),
        ToolDef(
            name="glob",
            description="Find files matching a glob pattern (e.g. **/*.py).",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern"},
                },
                "required": ["pattern"],
            },
        ),
        ToolDef(
            name="grep",
            description="Search file contents with regex. Uses ripgrep if available.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex search pattern"},
                    "glob": {"type": "string", "description": "File glob filter (e.g. *.py)"},
                },
                "required": ["pattern"],
            },
        ),
    ]


def mutation_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name="write_file",
            description="Write content to a file (creates parent dirs). Overwrites if exists.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from repo root"},
                    "content": {"type": "string", "description": "File content to write"},
                },
                "required": ["path", "content"],
            },
        ),
        ToolDef(
            name="edit_file",
            description="Replace a string in a file. old_string must be unique unless replace_all is true.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from repo root"},
                    "old_string": {"type": "string", "description": "Text to find"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        ),
    ]


READ_ONLY_FNS: dict[str, ToolFn] = {
    "read_file": _read_file,
    "list_dir": _list_dir,
    "glob": _glob,
    "grep": _grep,
}

MUTATION_FNS: dict[str, ToolFn] = {
    "write_file": _write_file,
    "edit_file": _edit_file,
}

CACHEABLE = {"read_file", "list_dir", "glob", "grep"}
