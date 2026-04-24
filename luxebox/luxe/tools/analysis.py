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
        ToolDef(
            name="typecheck",
            description=(
                "Run mypy (a real Python type checker) and return structured "
                "type errors. Prefer this over inferring types from grep. "
                "`path` defaults to repo root; `config_file` points at an "
                "existing `mypy.ini` / `pyproject.toml` section."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "config_file": {"type": "string"},
                },
                "required": [],
            },
        ),
        ToolDef(
            name="security_scan",
            description=(
                "Run bandit (Python security linter) and return findings. "
                "Catches deserialization, weak crypto, insecure subprocess, "
                "hardcoded credentials patterns. Prefer this over grepping "
                "for security patterns. Default filters out LOW-severity / "
                "LOW-confidence noise — raise `min_severity` to 'MEDIUM' for "
                "only material findings, or 'LOW' for everything."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "min_severity": {
                        "type": "string",
                        "enum": ["LOW", "MEDIUM", "HIGH"],
                        "default": "LOW",
                    },
                    "min_confidence": {
                        "type": "string",
                        "enum": ["LOW", "MEDIUM", "HIGH"],
                        "default": "MEDIUM",
                    },
                },
                "required": [],
            },
        ),
        ToolDef(
            name="deps_audit",
            description=(
                "Run pip-audit against installed Python dependencies (or a "
                "requirements file) and return known-CVE findings. `requirements` "
                "is optional — if set, audits the pinned file instead of the "
                "live environment."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "requirements": {"type": "string"},
                },
                "required": [],
            },
        ),
        ToolDef(
            name="security_taint",
            description=(
                "Run semgrep with its Python security rulesets against "
                "source and return findings. Unlike `security_scan` (bandit, "
                "which is pattern-based), semgrep does source→sanitizer→sink "
                "taint reasoning. USE THIS for `eval`/`exec`/`subprocess`/"
                "pickle/SQL-injection severity calls — semgrep correctly "
                "ignores sandboxed or non-user-controlled sinks that "
                "bandit would flag as LOW. First invocation may pull "
                "rules from the registry (needs network); cached after."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "config": {
                        "type": "string",
                        "description": (
                            "Semgrep ruleset — 'p/python' (default) covers "
                            "Flask/Django/language-level security. Also "
                            "useful: 'p/owasp-top-ten', 'p/cwe-top-25', "
                            "or a path to a local .yml rules file."
                        ),
                    },
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


_SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def typecheck(args: dict[str, Any]) -> tuple[Any, str | None]:
    path = (args.get("path") or ".").strip() or "."
    config_file = (args.get("config_file") or "").strip()

    cmd = ["mypy", "--show-column-numbers", "--no-error-summary", "--output", "json"]
    if config_file:
        cmd += ["--config-file", config_file]
    cmd.append(path)

    out, err = run_binary(
        cmd,
        cwd=fs.repo_root(),
        timeout_s=180,
        max_output_chars=256_000,
        missing_hint="uv sync --extra dev pulls it in, or `pip install mypy`.",
        allow_nonzero_exit=True,  # exit=1 when type errors exist
    )
    if err:
        return None, err

    findings: list[dict[str, Any]] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        findings.append({
            "file": _relpath(rec.get("file", "")),
            "line": rec.get("line"),
            "column": rec.get("column"),
            "severity": rec.get("severity", "error"),
            "code": rec.get("code", ""),
            "message": rec.get("message", ""),
        })
        if len(findings) >= MAX_FINDINGS:
            break

    payload: dict[str, Any] = {"findings": findings, "count": len(findings)}
    if not findings:
        payload["note"] = "no type errors"
    elif len(findings) >= MAX_FINDINGS:
        payload["truncated_at"] = MAX_FINDINGS
    return json.dumps(payload), None


def security_scan(args: dict[str, Any]) -> tuple[Any, str | None]:
    path = (args.get("path") or ".").strip() or "."
    min_sev = (args.get("min_severity") or "LOW").strip().upper()
    min_conf = (args.get("min_confidence") or "MEDIUM").strip().upper()
    min_sev_rank = _SEVERITY_RANK.get(min_sev, 0)
    min_conf_rank = _SEVERITY_RANK.get(min_conf, 1)

    cmd = ["bandit", "-q", "-r", "-f", "json", path]
    out, err = run_binary(
        cmd,
        cwd=fs.repo_root(),
        timeout_s=120,
        max_output_chars=512_000,
        missing_hint="uv sync --extra dev pulls it in, or `pip install bandit`.",
        allow_nonzero_exit=True,  # exit=1 when issues found
    )
    if err:
        return None, err

    if not out or not out.strip():
        return json.dumps({"findings": [], "count": 0, "note": "no output"}), None

    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        return None, f"bandit produced non-JSON output: {e}; head={out[:400]!r}"

    findings: list[dict[str, Any]] = []
    for item in data.get("results", []):
        if not isinstance(item, dict):
            continue
        sev = str(item.get("issue_severity", "LOW")).upper()
        conf = str(item.get("issue_confidence", "LOW")).upper()
        if _SEVERITY_RANK.get(sev, 0) < min_sev_rank:
            continue
        if _SEVERITY_RANK.get(conf, 0) < min_conf_rank:
            continue
        findings.append({
            "file": _relpath(item.get("filename", "")),
            "line": item.get("line_number"),
            "test_id": item.get("test_id", ""),
            "issue_text": item.get("issue_text", ""),
            "severity": sev,
            "confidence": conf,
            "more_info": item.get("more_info", ""),
        })
        if len(findings) >= MAX_FINDINGS:
            break

    payload: dict[str, Any] = {"findings": findings, "count": len(findings)}
    if not findings:
        payload["note"] = (
            f"no findings at severity>={min_sev} confidence>={min_conf} "
            "— pass `min_severity='LOW', min_confidence='LOW'` to widen"
        )
    elif len(findings) >= MAX_FINDINGS:
        payload["truncated_at"] = MAX_FINDINGS
    return json.dumps(payload), None


def deps_audit(args: dict[str, Any]) -> tuple[Any, str | None]:
    requirements = (args.get("requirements") or "").strip()

    cmd = ["pip-audit", "--format", "json", "--progress-spinner", "off"]
    if requirements:
        cmd += ["--requirement", requirements]
    out, err = run_binary(
        cmd,
        cwd=fs.repo_root(),
        timeout_s=120,
        max_output_chars=512_000,
        missing_hint="uv sync --extra dev pulls it in, or `pip install pip-audit`.",
        allow_nonzero_exit=True,  # exit=1 when vulns found
    )
    if err:
        return None, err
    if not out or not out.strip():
        return json.dumps({"findings": [], "count": 0, "note": "no output"}), None

    # pip-audit may prefix stdout with a banner before the JSON body.
    # Find the first '{' and parse from there.
    brace = out.find("{")
    if brace < 0:
        return json.dumps({"findings": [], "count": 0, "note": "no JSON body"}), None
    try:
        data = json.loads(out[brace:])
    except json.JSONDecodeError as e:
        return None, f"pip-audit produced non-JSON output: {e}; head={out[:400]!r}"

    findings: list[dict[str, Any]] = []
    for dep in data.get("dependencies", []):
        if not isinstance(dep, dict):
            continue
        vulns = dep.get("vulns") or []
        if not vulns:
            continue
        findings.append({
            "name": dep.get("name", ""),
            "version": dep.get("version", ""),
            "vulns": [
                {
                    "id": v.get("id", ""),
                    "fix_versions": v.get("fix_versions", []),
                    "description": v.get("description", ""),
                }
                for v in vulns if isinstance(v, dict)
            ],
        })
        if len(findings) >= MAX_FINDINGS:
            break

    payload: dict[str, Any] = {"findings": findings, "count": len(findings)}
    if not findings:
        payload["note"] = "no known-CVE dependencies"
    return json.dumps(payload), None


def security_taint(args: dict[str, Any]) -> tuple[Any, str | None]:
    path = (args.get("path") or ".").strip() or "."
    config = (args.get("config") or "p/python").strip() or "p/python"

    cmd = ["semgrep", "--config", config, "--json", "--quiet", path]
    out, err = run_binary(
        cmd,
        cwd=fs.repo_root(),
        timeout_s=300,  # semgrep can be slow on first run (rule download)
        max_output_chars=1_000_000,  # reports are verbose; cap post-parse
        missing_hint="uv sync --extra dev pulls it in, or `brew install semgrep`.",
        allow_nonzero_exit=True,  # exit=1 on findings
    )
    if err:
        return None, err
    if not out or not out.strip():
        return json.dumps({"findings": [], "count": 0, "note": "no output"}), None

    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        return None, f"semgrep produced non-JSON output: {e}; head={out[:400]!r}"

    findings: list[dict[str, Any]] = []
    for r in data.get("results", []):
        if not isinstance(r, dict):
            continue
        extra = r.get("extra") or {}
        meta = extra.get("metadata") or {}
        findings.append({
            "file": _relpath(r.get("path", "")),
            "line": (r.get("start") or {}).get("line"),
            "end_line": (r.get("end") or {}).get("line"),
            "check_id": r.get("check_id", ""),
            "severity": extra.get("severity", "INFO"),
            "confidence": meta.get("confidence", ""),
            "cwe": (meta.get("cwe") or [""])[0],
            "message": extra.get("message", "").strip(),
            "url": meta.get("source") or meta.get("shortlink") or "",
        })
        if len(findings) >= MAX_FINDINGS:
            break

    errors = [
        {"message": e.get("message", ""), "path": e.get("path", "")}
        for e in data.get("errors", [])
        if isinstance(e, dict)
    ]

    payload: dict[str, Any] = {"findings": findings, "count": len(findings)}
    if errors:
        payload["semgrep_errors"] = errors[:5]
    if not findings:
        payload["note"] = (
            f"no taint-reachable findings with config={config!r}"
        )
    elif len(findings) >= MAX_FINDINGS:
        payload["truncated_at"] = MAX_FINDINGS
    return json.dumps(payload), None


TOOL_FNS = {
    "lint": lint,
    "typecheck": typecheck,
    "security_scan": security_scan,
    "deps_audit": deps_audit,
    "security_taint": security_taint,
}
