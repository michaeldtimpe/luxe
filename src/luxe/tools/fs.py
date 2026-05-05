"""Filesystem tools — scoped to repo root for safety."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from luxe.sdd import SddParseError
from luxe.spec_resolver import resolve_chain
from luxe.tools.base import ToolDef, ToolFn

_REPO_ROOT: Path | None = None
_MAX_FILE_SIZE = 256 * 1024  # 256 KB read limit
_MAX_RESULTS = 150


# --- write-time honesty guards --------------------------------------------
# Catch the three failure modes Phase 2 surfaced — placeholder text,
# role-name leaks, mass-deletion overwrites — at the moment of write rather
# than after the PR is opened. Cheaper feedback loop for the model: the
# tool returns an error, the agent gets a chance to retry with real code.

# Multi-word placeholder coverage — the model has been seen evading the
# tight 1-word "your X code here" form by writing "your real listener code
# here". \w+(\s+\w+){0,5} allows up to 6 noun phrases between trigger words.
_PLACEHOLDER_PATTERNS = (
    re.compile(r"<paste\b[^<>]*\bhere\s*>", re.IGNORECASE),
    re.compile(
        r"(?://|#)\s*your\s+(?:real\s+|own\s+|actual\s+)?\w+(?:\s+\w+){0,5}\s+(?:code|here|implementation|logic)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?://|#)\s*(?:add|implement|insert|paste|reset|attach|wire|hook)\s+"
        r"(?:the\s+|a\s+|an\s+)?\w+(?:\s+\w+){0,5}\s+here\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?://|#)\s*(?:fill\s+in|put|place)\s+(?:the\s+|your\s+)?\w+(?:\s+\w+){0,3}\s+here\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?://|#)\s*todo:?\s*(?:implement|add|finish|complete|fill|wire|hook)\s",
               re.IGNORECASE),
    re.compile(r"(?://|#)\s*real\s+\w+(?:\s+\w+){0,3}\s+(?:goes|belongs)\s+here\b",
               re.IGNORECASE),
)
# Agent role names. Matching is fuzzy — the model has been seen writing
# `worker_read_r.py` to sneak past exact-stem matching, so we also flag any
# path component whose tokens (split on _ and -) contain a role-name token.
_ROLE_NAME_TOKENS = frozenset({
    "worker_read", "worker_code", "worker_analyze",
    "drafter", "coder", "verifier", "linter",
    "architect", "micro_architect", "synthesizer", "validator",
})
# Single-token leak detection — split path components on `_` and `-`, then
# look for these multi-word role names AS substrings of the joined token list.
_ROLE_FUZZY_NEEDLES = (
    "worker_read", "worker_code", "worker_analyze",
    "micro_architect",
    # Single-word roles get added with prefix/suffix flexibility below.
)
_ROLE_SINGLE_TOKENS = frozenset({
    "drafter", "verifier", "linter", "architect", "synthesizer", "validator",
    # NOTE: "coder" intentionally omitted — too common in legitimate names
    # ("encoder", "decoder", "transcoder"). Compose with prefixes if needed.
})
# Mass-deletion thresholds: refuse to collapse a non-trivial file into a
# stub. Old must have ≥ N lines, new must have ≤ M lines, AND the size drop
# must be at least 10× — otherwise legitimate refactor-shrinks still pass.
_MASS_DELETE_OLD_LINES = 50
_MASS_DELETE_NEW_LINES = 5
_MASS_DELETE_RATIO = 10.0


def _check_placeholder_text(content: str) -> str | None:
    """Return error string if `content` contains a placeholder pattern."""
    for pat in _PLACEHOLDER_PATTERNS:
        m = pat.search(content)
        if m:
            return (
                f"refusing to write placeholder text {m.group(0)[:80]!r}. "
                "Replace with the real implementation, or read the existing "
                "code first if you don't know what to write."
            )
    return None


def _check_role_path(rel_path: str) -> str | None:
    """Return error if any path component contains an agent role label —
    fuzzy: catches `worker_read.js`, `worker_read_r.py`, `drafter_helper.js`,
    `my_verifier.py`, etc. Doesn't catch substrings inside other words
    (`coder` inside `encoder.py` is fine).
    """
    for part in rel_path.split("/"):
        stem = part.split(".", 1)[0].lower()
        # Normalize: tokenize on _ and -; reassemble for substring match
        # against multi-word needles like "worker_read".
        tokens = re.split(r"[-_]+", stem)
        joined = "_".join(tokens)
        # Multi-word role labels (substring match against rejoined form).
        for needle in _ROLE_FUZZY_NEEDLES:
            if needle in joined:
                return (
                    f"refusing to write to {rel_path!r}: path contains agent "
                    f"role label {needle!r}. Agent role names are internal "
                    "orchestration concepts; pick a project-appropriate name."
                )
        # Single-word role labels (must appear as a discrete token, not a
        # substring of an unrelated word).
        for tok in tokens:
            if tok in _ROLE_SINGLE_TOKENS:
                return (
                    f"refusing to write to {rel_path!r}: path token {tok!r} "
                    "is an agent role label. Agent role names are internal "
                    "orchestration concepts; pick a project-appropriate name."
                )
    return None


def _check_mass_deletion(old_text: str, new_text: str, rel: str) -> str | None:
    """Refuse to collapse a substantial file into a tiny stub."""
    old_lines = old_text.count("\n") + (1 if old_text and not old_text.endswith("\n") else 0)
    new_lines = new_text.count("\n") + (1 if new_text and not new_text.endswith("\n") else 0)
    if old_lines < _MASS_DELETE_OLD_LINES:
        return None
    if new_lines > _MASS_DELETE_NEW_LINES:
        return None
    if new_lines > 0 and (old_lines / new_lines) < _MASS_DELETE_RATIO:
        return None
    return (
        f"refusing to overwrite {rel!r}: would collapse {old_lines}-line "
        f"file to {new_lines}-line stub (mass-deletion blocked). Use "
        "edit_file for surgical changes, or write the FULL replacement "
        "content if rewriting is genuinely intended."
    )


def set_repo_root(path: str | Path) -> None:
    global _REPO_ROOT
    _REPO_ROOT = Path(path).resolve()


def get_repo_root() -> Path | None:
    """Return the currently-set repo root, or None if not yet configured.

    Use this from sibling tool modules (shell.py, git.py, analysis.py)
    instead of `from luxe.tools.fs import _REPO_ROOT`. The import-style
    binds the module-level name once at import time, so subsequent calls
    to set_repo_root() don't propagate — any tool using the imported
    name silently fails with "Repo root not set" forever. The getter
    closes that latent bug (caught by test_tools.py's bash chain-rejection
    suite on 2026-05-02; all three sibling tool modules switched to it
    in the same commit).
    """
    return _REPO_ROOT


def _safe(rel: str) -> Path:
    if _REPO_ROOT is None:
        raise RuntimeError("Repo root not set — call set_repo_root() first")
    resolved = (_REPO_ROOT / rel).resolve()
    if not str(resolved).startswith(str(_REPO_ROOT)):
        raise PermissionError(f"Path escapes repo root: {rel}")
    return resolved


def _check_spec_forbids(rel: str) -> str | None:
    """Return error if `rel` matches a `.sdd` `Forbids:` glob in the chain.

    SpecDD Lever 2 enforcement: pre-write tool-side guard. The model
    cannot evade by renaming once a `Forbids:` rule exists in an
    ancestor `.sdd` — the rule fires on every write attempt regardless
    of how the path was constructed.

    Returns None when:
      - no repo root is set (test envs that bypass set_repo_root)
      - no `.sdd` files exist in the chain (the common case for repos
        that haven't adopted SpecDD)
      - the path is allowed by the chain

    Returns a structured error string with the offending `.sdd` path
    and the matching glob when the path is forbidden.

    A malformed `.sdd` upstream raises SddParseError; we convert that
    to a tool-level error so the model sees one actionable message
    rather than a stack trace. Repeat-fires are fine — broken `.sdd`
    is an authoring bug, not a bench-loop concern.
    """
    if _REPO_ROOT is None:
        return None
    target = (_REPO_ROOT / rel).resolve()
    try:
        chain = resolve_chain(_REPO_ROOT, target)
    except SddParseError as e:
        # NOTE: must come before the ValueError catch — SddParseError
        # subclasses ValueError, so the order matters.
        return f"Cannot evaluate Forbids: malformed .sdd — {e}"
    except ValueError:
        # target outside repo_root; _safe() will reject this independently
        return None

    forbidden, sdd, glob = chain.is_forbidden(rel)
    if not forbidden or sdd is None:
        return None
    try:
        sdd_rel = sdd.path.relative_to(_REPO_ROOT)
    except ValueError:
        sdd_rel = sdd.path
    return (
        f"refusing to write {rel!r}: forbidden by {sdd_rel} "
        f"(matches glob {glob!r}). This worker is scoped by .sdd "
        f"contracts; do not write files outside the allowed paths."
    )


def _read_file(args: dict[str, Any]) -> tuple[str, str | None]:
    path = _safe(args["path"])
    if not path.is_file():
        return "", f"File not found: {args['path']}"
    size = path.stat().st_size
    if size > _MAX_FILE_SIZE:
        return "", f"File too large ({size} bytes, limit {_MAX_FILE_SIZE})"
    # Reject obvious binary files — reading them with errors="replace" returns
    # gigabytes of garbage that pollutes the model's context. Null bytes in
    # the first 8 KB is a strong signal: text formats don't contain them, and
    # PNG/JPG/zip/elf/etc. all do. Lets the model see UTF-8/UTF-16 source
    # files without false positives (those don't have null bytes in code).
    try:
        head = path.read_bytes()[:8192]
    except OSError as e:
        return "", str(e)
    if b"\x00" in head:
        return "", (
            f"File appears to be binary ({args['path']}): null bytes in "
            f"first 8 KB. Use ls / file / a hex dumper if you need to "
            "inspect binary content; this tool is for text source only."
        )
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
    rel = args["path"]
    content = args["content"]

    # Honesty guards — applied before any I/O so a refusal costs nothing.
    if (err := _check_role_path(rel)):
        return "", err
    if (err := _check_placeholder_text(content)):
        return "", err
    # SpecDD Lever 2: tool-side Forbids enforcement. Cheap directory
    # walk; no-op when no `.sdd` exists in the chain.
    if (err := _check_spec_forbids(rel)):
        return "", err

    path = _safe(rel)
    # Mass-deletion check needs the existing content (if file exists).
    if path.is_file():
        try:
            existing = path.read_text(errors="replace")
        except OSError:
            existing = ""
        if (err := _check_mass_deletion(existing, content, rel)):
            return "", err

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(content)
    except Exception as e:
        return "", str(e)
    return f"Wrote {len(content)} bytes to {rel}", None


def _edit_file(args: dict[str, Any]) -> tuple[str, str | None]:
    rel = args["path"]
    if (err := _check_role_path(rel)):
        return "", err
    # SpecDD Lever 2: tool-side Forbids — symmetric with _write_file.
    if (err := _check_spec_forbids(rel)):
        return "", err

    path = _safe(rel)
    if not path.is_file():
        return "", f"File not found: {rel}"
    try:
        text = path.read_text()
    except Exception as e:
        return "", str(e)
    old = args["old_string"]
    new = args["new_string"]

    # Block placeholder text from sneaking in via edits.
    if (err := _check_placeholder_text(new)):
        return "", err

    count = text.count(old)
    if count == 0:
        return "", f"old_string not found in {rel}"
    if count > 1 and not args.get("replace_all", False):
        return "", f"old_string matches {count} times — use replace_all or provide more context"
    new_text = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)

    if (err := _check_mass_deletion(text, new_text, rel)):
        return "", err

    path.write_text(new_text)
    return f"Edited {rel} ({count} replacement{'s' if count > 1 else ''})", None


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
