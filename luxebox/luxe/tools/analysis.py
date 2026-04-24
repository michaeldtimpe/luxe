"""Static-analysis tool wrappers for code / review / refactor agents.

Prefer these over grepping for patterns. A grep match is evidence;
an analyzer finding is a finding — already located, already classified,
already severity-tagged. The model's job is to orchestrate these tools
and interpret their output, not re-derive them with regex.

Each tool shells out to a system binary via `run_binary` and returns
structured JSON (`[{...}, ...]` or `"no findings"`). Missing binaries
surface a `<binary> not installed. <hint>` message so the agent can
adapt instead of crashing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from harness.backends import ToolDef

from luxe.tools import fs
from luxe.tools._subprocess import run_binary

MAX_FINDINGS = 150  # per-tool cap; matches fs.MAX_GREP_MATCHES sensibility


def tool_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name="lint",
            description=(
                "Run ruff (a real Python linter) against source files and "
                "return structured findings. Prefer this over grepping for "
                "lint patterns (unused imports, bare-except, style, bugbear). "
                "`path` defaults to the repo root. `select` narrows rules "
                '(e.g. "F,E,W,B" for pyflakes+pycodestyle+bugbear); '
                "`ignore` excludes specific codes or paths."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "select": {"type": "string"},
                    "ignore": {"type": "string"},
                },
                "required": [],
            },
        ),
    ]


def _relpath(abs_or_rel: str) -> str:
    """Normalize ruff's absolute paths back to repo-relative for
    consistency with grep/read_file output."""
    p = Path(abs_or_rel)
    root = fs.repo_root()
    try:
        return p.resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return abs_or_rel


def lint(args: dict[str, Any]) -> tuple[Any, str | None]:
    path = (args.get("path") or ".").strip() or "."
    select = (args.get("select") or "").strip()
    ignore = (args.get("ignore") or "").strip()

    cmd = ["ruff", "check", "--output-format=json", "--no-fix"]
    if select:
        cmd += ["--select", select]
    if ignore:
        cmd += ["--ignore", ignore]
    cmd.append(path)

    out, err = run_binary(
        cmd,
        cwd=fs.repo_root(),
        timeout_s=60,
        max_output_chars=256_000,  # JSON can be large; we cap post-parse
        missing_hint="uv sync --extra dev pulls it in, or `pip install ruff`.",
        allow_nonzero_exit=True,  # exit=1 means findings exist — not an error
    )
    if err:
        return None, err
    if not out or not out.strip():
        return json.dumps({"findings": [], "note": "no findings"}), None

    try:
        raw = json.loads(out)
    except json.JSONDecodeError as e:
        # Ruff usually emits valid JSON even with diagnostics; if it didn't,
        # hand back the raw tail so the agent can at least see what happened.
        return None, f"ruff produced non-JSON output: {e}; head={out[:400]!r}"

    findings = [
        {
            "file": _relpath(item.get("filename", "")),
            "line": (item.get("location") or {}).get("row"),
            "column": (item.get("location") or {}).get("column"),
            "code": item.get("code"),
            "message": item.get("message", ""),
            "url": item.get("url") or "",
        }
        for item in raw
        if isinstance(item, dict)
    ]

    truncated = len(findings) > MAX_FINDINGS
    if truncated:
        findings = findings[:MAX_FINDINGS]

    payload: dict[str, Any] = {"findings": findings, "count": len(findings)}
    if truncated:
        payload["truncated_at"] = MAX_FINDINGS
        payload["note"] = (
            f"showing first {MAX_FINDINGS} findings; narrow with `select=` "
            "or a more specific `path` to see the rest"
        )
    elif not findings:
        payload["note"] = "no findings"
    return json.dumps(payload), None


TOOL_FNS = {
    "lint": lint,
}
