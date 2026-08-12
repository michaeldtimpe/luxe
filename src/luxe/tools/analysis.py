"""Static analysis tools — language-gated, delegates to real linters."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from typing import Any

from luxe.tools.base import ToolDef, ToolFn
from luxe.tools.fs import get_repo_root

_MAX_FINDINGS = 150
_TIMEOUT = 60


def _skipped(tool: str) -> tuple[str, str | None]:
    """A SUCCESSFUL, machine-readable 'not run' result (B3).

    Returning an error here is dangerous: agents read 'Tool not found' as
    'lint passed' and proceed on a false signal. A structured `status:skipped`
    makes the absence explicit and parseable without derailing the loop.
    """
    payload = {
        "status": "skipped",
        "reason": (f"{tool} is not available (not on PATH, not importable as a "
                   f"Python module, and uvx unavailable). Install it (e.g. "
                   f"`pip install {tool}`) or run via uvx to enable this check."),
        "findings": [],
        "count": 0,
    }
    return json.dumps(payload, indent=2), None


def _resolve(tool: str, module: str | None = None,
             allow_uvx: bool = False) -> list[str] | None:
    """Resolve an argv prefix that runs `tool`, or None if unavailable.

    Order (no installs, ever): PATH binary → `python -m <module>` (only if the
    module is importable in THIS interpreter) → `uvx <tool>` (ephemeral, never
    touches the project venv). Provisioning stays a human concern.
    """
    if shutil.which(tool):
        return [tool]
    if module and importlib.util.find_spec(module) is not None:
        return [sys.executable, "-m", module]
    if allow_uvx and shutil.which("uvx"):
        return ["uvx", tool]
    return None


def _crashed(cmd: list[str], proc: subprocess.CompletedProcess) -> tuple[str, str | None]:
    """A SUCCESSFUL, machine-readable 'the check did not run' result.

    Same reasoning as `_skipped`, one failure mode over: a linter that exits
    non-zero having produced nothing parseable did not find zero problems, it
    fell over (bad config, a repo that doesn't compile, a missing plugin). The
    old code reported `{"status": "ok", "findings": [], "count": 0}` for that,
    which reads as "lint passed" — precisely the false signal `_skipped`
    exists to prevent.
    """
    detail = (proc.stderr or proc.stdout or "").strip()
    payload = {
        "status": "error",
        "exit_code": proc.returncode,
        "stderr": detail[-2000:],
        "reason": (f"{cmd[0]} exited {proc.returncode} without producing any "
                   f"findings — the check did NOT pass, it failed to run. "
                   f"Fix the reported error (often a config or a file the "
                   f"tool cannot parse) and call this tool again."),
    }
    return json.dumps(payload, indent=2), None


def _run_tool(cmd: list[str], parse_json: bool = False) -> tuple[str, str | None]:
    repo_root = get_repo_root()
    if repo_root is None:
        return "", "Repo root not set"
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            # A linter that prints a non-UTF-8 byte (a source file in another
            # encoding quoted back in a diagnostic) must not take its whole
            # report down with a UnicodeDecodeError.
            errors="replace",
            # The analyzers are non-interactive; inheriting luxe's stdin lets
            # one of them consume input meant for the session (see tools/shell.py).
            stdin=subprocess.DEVNULL,
            cwd=repo_root, timeout=_TIMEOUT,
        )
        output = proc.stdout or proc.stderr
        if parse_json:
            try:
                data = json.loads(output)
                if isinstance(data, list):
                    if not data and proc.returncode != 0:
                        # "no findings" and "exited non-zero" cannot both be
                        # true of a healthy run.
                        return _crashed(cmd, proc)
                    total = len(data)
                    data = data[:_MAX_FINDINGS]
                    payload: dict[str, Any] = {"status": "ok", "findings": data,
                                               "count": len(data)}
                    if total > _MAX_FINDINGS:
                        # Without this the count reads as authoritative: 150
                        # findings and 1,286 findings were the same result.
                        payload["truncated"] = True
                        payload["total"] = total
                    return json.dumps(payload, indent=2), None
                return json.dumps({"status": "ok", "findings": data,
                                   "count": len(data)}, indent=2), None
            except json.JSONDecodeError:
                # A tool asked for JSON that exits non-zero WITHOUT emitting
                # any is a tool that crashed; its stderr is a message, not a
                # findings list.
                if proc.returncode != 0:
                    return _crashed(cmd, proc)
        all_lines = output.strip().splitlines()
        lines = all_lines[:_MAX_FINDINGS]
        # A non-zero exit WITH findings is ordinary (mypy exits 1 on type
        # errors); a non-zero exit with nothing to show is a failure.
        if proc.returncode != 0 and not lines:
            return _crashed(cmd, proc)
        payload = {"status": "ok", "findings": lines, "count": len(lines)}
        if len(all_lines) > _MAX_FINDINGS:
            payload["truncated"] = True
            payload["total"] = len(all_lines)
        return json.dumps(payload, indent=2), None
    except FileNotFoundError:
        # Resolution should prevent this, but degrade structurally if it slips.
        return _skipped(cmd[0])
    except subprocess.TimeoutExpired:
        return "", f"{cmd[0]} timed out after {_TIMEOUT}s"


def _run_resolved(tool: str, tail: list[str], *, module: str | None = None,
                  allow_uvx: bool = False, parse_json: bool = False) -> tuple[str, str | None]:
    prefix = _resolve(tool, module=module, allow_uvx=allow_uvx)
    if prefix is None:
        return _skipped(tool)
    return _run_tool(prefix + tail, parse_json=parse_json)


def _lint(args: dict[str, Any]) -> tuple[str, str | None]:
    path = args.get("path", ".")
    return _run_resolved("ruff", ["check", "--output-format=json", path],
                         module="ruff", allow_uvx=True, parse_json=True)


def _typecheck(args: dict[str, Any]) -> tuple[str, str | None]:
    path = args.get("path", ".")
    return _run_resolved("mypy", ["--no-color-output", "--no-error-summary", path],
                         module="mypy", allow_uvx=True)


def _security_scan(args: dict[str, Any]) -> tuple[str, str | None]:
    path = args.get("path", ".")
    return _run_resolved("bandit", ["-r", "-f", "json", path],
                         module="bandit", allow_uvx=True, parse_json=True)


def _deps_audit(args: dict[str, Any]) -> tuple[str, str | None]:
    return _run_resolved("pip-audit", ["--format=json"],
                         module="pip_audit", allow_uvx=True, parse_json=True)


def _lint_js(args: dict[str, Any]) -> tuple[str, str | None]:
    path = args.get("path", ".")
    return _run_resolved("npx", ["eslint", "--format=json", path], parse_json=True)


def _typecheck_ts(args: dict[str, Any]) -> tuple[str, str | None]:
    return _run_resolved("npx", ["tsc", "--noEmit", "--pretty", "false"])


def _lint_rust(args: dict[str, Any]) -> tuple[str, str | None]:
    return _run_resolved("cargo", ["clippy", "--message-format=json"], parse_json=True)


def _vet_go(args: dict[str, Any]) -> tuple[str, str | None]:
    return _run_resolved("go", ["vet", "./..."])


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
