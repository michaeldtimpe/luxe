"""Static analysis tools — language-gated, delegates to real linters."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from luxe.tools.base import ToolDef, ToolFn
from luxe.tools.fs import get_repo_root

_MAX_FINDINGS = 150
_TIMEOUT = 60


def _run_tool(cmd: list[str], parse_json: bool = False) -> tuple[str, str | None]:
    repo_root = get_repo_root()
    if repo_root is None:
        return "", "Repo root not set"
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=repo_root, timeout=_TIMEOUT,
        )
        output = proc.stdout or proc.stderr
        if parse_json:
            try:
                data = json.loads(output)
                if isinstance(data, list):
                    data = data[:_MAX_FINDINGS]
                return json.dumps({"findings": data, "count": len(data)}, indent=2), None
            except json.JSONDecodeError:
                pass
        lines = output.strip().splitlines()[:_MAX_FINDINGS]
        return json.dumps({"findings": lines, "count": len(lines)}, indent=2), None
    except FileNotFoundError:
        return "", f"Tool not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return "", f"{cmd[0]} timed out after {_TIMEOUT}s"


def _lint(args: dict[str, Any]) -> tuple[str, str | None]:
    path = args.get("path", ".")
    return _run_tool(["ruff", "check", "--output-format=json", path], parse_json=True)


def _typecheck(args: dict[str, Any]) -> tuple[str, str | None]:
    path = args.get("path", ".")
    return _run_tool(["mypy", "--no-color-output", "--no-error-summary", path])


def _security_scan(args: dict[str, Any]) -> tuple[str, str | None]:
    path = args.get("path", ".")
    return _run_tool(["bandit", "-r", "-f", "json", path], parse_json=True)


def _deps_audit(args: dict[str, Any]) -> tuple[str, str | None]:
    return _run_tool(["pip-audit", "--format=json"], parse_json=True)


def _lint_js(args: dict[str, Any]) -> tuple[str, str | None]:
    path = args.get("path", ".")
    return _run_tool(["npx", "eslint", "--format=json", path], parse_json=True)


def _typecheck_ts(args: dict[str, Any]) -> tuple[str, str | None]:
    return _run_tool(["npx", "tsc", "--noEmit", "--pretty", "false"])


def _lint_rust(args: dict[str, Any]) -> tuple[str, str | None]:
    return _run_tool(["cargo", "clippy", "--message-format=json"], parse_json=True)


def _vet_go(args: dict[str, Any]) -> tuple[str, str | None]:
    return _run_tool(["go", "vet", "./..."])


_ANALYZERS: dict[str, dict[str, Any]] = {
    "lint": {
        "fn": _lint,
        "langs": {"python"},
        "desc": "Run ruff linter on Python code.",
    },
    "typecheck": {
        "fn": _typecheck,
        "langs": {"python"},
        "desc": "Run mypy type checker on Python code.",
    },
    "security_scan": {
        "fn": _security_scan,
        "langs": {"python"},
        "desc": "Run bandit security scanner on Python code.",
    },
    "deps_audit": {
        "fn": _deps_audit,
        "langs": {"python"},
        "desc": "Audit Python dependencies for known vulnerabilities.",
    },
    "lint_js": {
        "fn": _lint_js,
        "langs": {"javascript", "typescript"},
        "desc": "Run ESLint on JavaScript/TypeScript code.",
    },
    "typecheck_ts": {
        "fn": _typecheck_ts,
        "langs": {"typescript"},
        "desc": "Run TypeScript compiler in check mode.",
    },
    "lint_rust": {
        "fn": _lint_rust,
        "langs": {"rust"},
        "desc": "Run Clippy on Rust code.",
    },
    "vet_go": {
        "fn": _vet_go,
        "langs": {"go"},
        "desc": "Run go vet on Go code.",
    },
}

_PATH_PARAM = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path to analyze (default: repo root)"},
    },
    "required": [],
}

_NO_PARAM = {"type": "object", "properties": {}, "required": []}


def tool_defs(languages: frozenset[str] | None = None) -> list[ToolDef]:
    defs = []
    for name, info in _ANALYZERS.items():
        if languages and not info["langs"] & languages:
            continue
        has_path = name not in {"deps_audit", "typecheck_ts", "vet_go"}
        defs.append(ToolDef(
            name=name,
            description=info["desc"],
            parameters=_PATH_PARAM if has_path else _NO_PARAM,
        ))
    return defs


def tool_fns(languages: frozenset[str] | None = None) -> dict[str, ToolFn]:
    fns = {}
    for name, info in _ANALYZERS.items():
        if languages and not info["langs"] & languages:
            continue
        fns[name] = info["fn"]
    return fns


CACHEABLE = set(_ANALYZERS.keys())
