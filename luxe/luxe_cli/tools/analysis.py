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
import re
from pathlib import Path
from typing import Any

from harness.backends import ToolDef

from luxe_cli.tools import fs
from luxe_cli.tools._subprocess import run_binary

MAX_FINDINGS = 150  # per-tool cap; matches fs.MAX_GREP_MATCHES sensibility


_LANG_FAMILY = {
    "lint":           "python",
    "typecheck":      "python",
    "security_scan":  "python",
    "deps_audit":     "python",
    "security_taint": "python",
    "secrets_scan":   "*",        # always included — creds appear in any language
    "lint_js":        "javascript",
    "typecheck_ts":   "javascript",
    "lint_rust":      "rust",
    "vet_go":         "go",
}


def _language_match(tool_name: str, languages: frozenset[str] | None) -> bool:
    """Decide whether to expose `tool_name` given the repo's language
    breakdown. None / empty → include all (unknown repo is conservative).
    The `*` family marker means language-agnostic (secrets_scan)."""
    if not languages:
        return True
    family = _LANG_FAMILY.get(tool_name)
    if family is None or family == "*":
        return True
    return family in languages


def tool_defs(languages: frozenset[str] | None = None) -> list[ToolDef]:
    """Tool definitions for static analyzers. When `languages` is a set
    of language names (as they appear in RepoSurvey.language_breakdown),
    gate out analyzers whose language isn't represented. `secrets_scan`
    is always included. Pass None for the unfiltered surface."""
    all_defs = _all_defs()
    return [d for d in all_defs if _language_match(d.name, languages)]


def _all_defs() -> list[ToolDef]:
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
            name="lint_js",
            description=(
                "Run ESLint against JavaScript/TypeScript source and "
                "return structured findings. Requires eslint on PATH or "
                "in the repo's node_modules/.bin. `path` defaults to "
                "the repo root. The tool reports 'not a JS/TS project' "
                "if no package.json is found at the scanned path."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                },
                "required": [],
            },
        ),
        ToolDef(
            name="typecheck_ts",
            description=(
                "Run `tsc --noEmit` for TypeScript type checking. "
                "Requires tsconfig.json at `path` (or a parent). "
                "Reports 'not a TS project' if none found."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                },
                "required": [],
            },
        ),
        ToolDef(
            name="lint_rust",
            description=(
                "Run `cargo clippy` (Rust linter) with JSON output and "
                "return structured findings. Requires Cargo.toml at the "
                "scanned path and a clippy-capable toolchain. Reports "
                "'not a Rust project' otherwise."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                },
                "required": [],
            },
        ),
        ToolDef(
            name="vet_go",
            description=(
                "Run `go vet ./...` against a Go module. Requires "
                "go.mod at the scanned path. Also runs `staticcheck` "
                "if installed for deeper checks. Reports 'not a Go "
                "project' if no go.mod found."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                },
                "required": [],
            },
        ),
        ToolDef(
            name="secrets_scan",
            description=(
                "Run gitleaks against files on disk and return findings of "
                "hardcoded secrets (AWS/GCP/Azure keys, GitHub/GitLab/"
                "Slack/Stripe tokens, private keys, generic high-entropy "
                "strings). Prefer this over grepping for `password|secret|"
                "api_key` — gitleaks has ~200 rules with entropy checks "
                "and fewer false positives. Scans only tracked+untracked "
                "files on disk by default; set `include_history=true` for "
                "a deep git-history pass (much slower)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "include_history": {"type": "boolean", "default": False},
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


def _project_marker(path: Path, markers: tuple[str, ...]) -> Path | None:
    """Return the directory containing any of `markers`, searching
    `path` and walking upward. None when no marker is found."""
    cur = path.resolve()
    root = fs.repo_root()
    # Stop at repo_root or the filesystem root, whichever comes first.
    while True:
        for m in markers:
            if (cur / m).exists():
                return cur
        if cur == root or cur.parent == cur:
            return None
        cur = cur.parent


def lint_js(args: dict[str, Any]) -> tuple[Any, str | None]:
    path = (args.get("path") or ".").strip() or "."
    scan_path = (fs.repo_root() / path).resolve()
    marker = _project_marker(scan_path, ("package.json",))
    if marker is None:
        return json.dumps({
            "findings": [], "count": 0, "note": "not a JS/TS project",
        }), None

    cmd = ["eslint", "--format", "json", path]
    out, err = run_binary(
        cmd,
        cwd=fs.repo_root(),
        timeout_s=120,
        max_output_chars=512_000,
        missing_hint="`npm install --save-dev eslint` or install globally.",
        allow_nonzero_exit=True,  # exit=1 when lint issues found
    )
    if err:
        return None, err
    if not out or not out.strip():
        return json.dumps({"findings": [], "count": 0, "note": "no output"}), None
    try:
        raw = json.loads(out)
    except json.JSONDecodeError as e:
        return None, f"eslint produced non-JSON output: {e}; head={out[:400]!r}"
    findings: list[dict[str, Any]] = []
    for file_result in raw:
        if not isinstance(file_result, dict):
            continue
        fpath = _relpath(file_result.get("filePath", ""))
        for m in file_result.get("messages", []) or []:
            if not isinstance(m, dict):
                continue
            findings.append({
                "file": fpath,
                "line": m.get("line"),
                "column": m.get("column"),
                "severity": "error" if m.get("severity") == 2 else "warning",
                "rule": m.get("ruleId") or "",
                "message": m.get("message", ""),
            })
            if len(findings) >= MAX_FINDINGS:
                break
        if len(findings) >= MAX_FINDINGS:
            break
    payload: dict[str, Any] = {"findings": findings, "count": len(findings)}
    if not findings:
        payload["note"] = "no findings"
    elif len(findings) >= MAX_FINDINGS:
        payload["truncated_at"] = MAX_FINDINGS
    return json.dumps(payload), None


def typecheck_ts(args: dict[str, Any]) -> tuple[Any, str | None]:
    path = (args.get("path") or ".").strip() or "."
    scan_path = (fs.repo_root() / path).resolve()
    marker = _project_marker(scan_path, ("tsconfig.json",))
    if marker is None:
        return json.dumps({
            "findings": [], "count": 0, "note": "not a TS project",
        }), None

    cmd = ["tsc", "--noEmit", "--pretty", "false", "--project", str(marker)]
    out, err = run_binary(
        cmd,
        cwd=fs.repo_root(),
        timeout_s=180,
        max_output_chars=256_000,
        missing_hint="`npm install --save-dev typescript` or install globally.",
        allow_nonzero_exit=True,  # exit=1/2 when type errors found
    )
    if err:
        return None, err
    # tsc output format: `path(line,col): error TSXXXX: message`
    findings: list[dict[str, Any]] = []
    tsc_re = re.compile(
        r"^(.*?)\((\d+),(\d+)\):\s*(error|warning)\s+(TS\d+):\s*(.*)$"
    )
    for line in (out or "").splitlines():
        m = tsc_re.match(line)
        if not m:
            continue
        findings.append({
            "file": _relpath(m.group(1)),
            "line": int(m.group(2)),
            "column": int(m.group(3)),
            "severity": m.group(4),
            "code": m.group(5),
            "message": m.group(6),
        })
        if len(findings) >= MAX_FINDINGS:
            break
    payload: dict[str, Any] = {"findings": findings, "count": len(findings)}
    if not findings:
        payload["note"] = "no type errors"
    elif len(findings) >= MAX_FINDINGS:
        payload["truncated_at"] = MAX_FINDINGS
    return json.dumps(payload), None


def lint_rust(args: dict[str, Any]) -> tuple[Any, str | None]:
    path = (args.get("path") or ".").strip() or "."
    scan_path = (fs.repo_root() / path).resolve()
    marker = _project_marker(scan_path, ("Cargo.toml",))
    if marker is None:
        return json.dumps({
            "findings": [], "count": 0, "note": "not a Rust project",
        }), None

    cmd = [
        "cargo", "clippy", "--message-format=json",
        "--manifest-path", str(marker / "Cargo.toml"),
        "--quiet",
    ]
    out, err = run_binary(
        cmd,
        cwd=fs.repo_root(),
        timeout_s=300,
        max_output_chars=1_000_000,
        missing_hint="`rustup component add clippy`.",
        allow_nonzero_exit=True,
    )
    if err:
        return None, err
    findings: list[dict[str, Any]] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("reason") != "compiler-message":
            continue
        msg = rec.get("message") or {}
        if not isinstance(msg, dict):
            continue
        level = msg.get("level", "")
        if level not in ("warning", "error"):
            continue
        spans = msg.get("spans") or []
        primary = next(
            (s for s in spans if isinstance(s, dict) and s.get("is_primary")),
            spans[0] if spans else None,
        )
        if not primary:
            continue
        findings.append({
            "file": _relpath(primary.get("file_name", "")),
            "line": primary.get("line_start"),
            "column": primary.get("column_start"),
            "severity": level,
            "code": (msg.get("code") or {}).get("code", "") if isinstance(msg.get("code"), dict) else "",
            "message": msg.get("message", ""),
        })
        if len(findings) >= MAX_FINDINGS:
            break
    payload: dict[str, Any] = {"findings": findings, "count": len(findings)}
    if not findings:
        payload["note"] = "no clippy findings"
    elif len(findings) >= MAX_FINDINGS:
        payload["truncated_at"] = MAX_FINDINGS
    return json.dumps(payload), None


def vet_go(args: dict[str, Any]) -> tuple[Any, str | None]:
    path = (args.get("path") or ".").strip() or "."
    scan_path = (fs.repo_root() / path).resolve()
    marker = _project_marker(scan_path, ("go.mod",))
    if marker is None:
        return json.dumps({
            "findings": [], "count": 0, "note": "not a Go project",
        }), None

    # `go vet` emits diagnostics on stderr. `run_binary` captures
    # stdout only, so fall back to a dedicated subprocess for this
    # tool to capture both streams — keep the helper uniform
    # elsewhere but handle Go's quirk locally.
    import subprocess
    try:
        res = subprocess.run(  # noqa: S603
            ["go", "vet", "./..."],
            cwd=str(marker),
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError:
        return None, "go not installed. `brew install go`."
    except subprocess.TimeoutExpired:
        return None, "go vet timed out after 180s"

    lines = (res.stdout + "\n" + res.stderr).splitlines()
    vet_re = re.compile(r"^(.*?):(\d+):(?:(\d+):)?\s*(.*)$")
    findings: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("#", "go:")):
            continue
        m = vet_re.match(line)
        if not m:
            continue
        findings.append({
            "file": _relpath(m.group(1)),
            "line": int(m.group(2)),
            "column": int(m.group(3)) if m.group(3) else None,
            "severity": "warning",
            "message": m.group(4),
        })
        if len(findings) >= MAX_FINDINGS:
            break
    payload: dict[str, Any] = {"findings": findings, "count": len(findings)}
    if not findings:
        payload["note"] = "no vet findings"
    elif len(findings) >= MAX_FINDINGS:
        payload["truncated_at"] = MAX_FINDINGS
    return json.dumps(payload), None


def secrets_scan(args: dict[str, Any]) -> tuple[Any, str | None]:
    path = (args.get("path") or ".").strip() or "."
    include_history = bool(args.get("include_history", False))

    cmd = [
        "gitleaks", "detect",
        "--report-format", "json", "--report-path", "-",
        "--redact=100",  # never leak actual secret bytes into the model
        "--source", path,
    ]
    if not include_history:
        # Scan files on disk (tracked + untracked) rather than crawling
        # git history — much faster, covers the current state which is
        # what a review cares about.
        cmd.append("--no-git")

    # gitleaks logs a banner to stderr and returns exit 1 when leaks are
    # found; allow_nonzero_exit so that's treated as a finding, not an
    # error.
    out, err = run_binary(
        cmd,
        cwd=fs.repo_root(),
        timeout_s=300 if include_history else 120,
        max_output_chars=512_000,
        missing_hint="brew install gitleaks.",
        allow_nonzero_exit=True,
    )
    if err:
        return None, err
    if not out or not out.strip():
        return json.dumps({"findings": [], "count": 0, "note": "no output"}), None

    try:
        raw = json.loads(out)
    except json.JSONDecodeError as e:
        return None, f"gitleaks produced non-JSON output: {e}; head={out[:400]!r}"

    if not isinstance(raw, list):
        return json.dumps({"findings": [], "count": 0}), None

    findings: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        findings.append({
            "file": _relpath(item.get("File", "")),
            "line": item.get("StartLine"),
            "end_line": item.get("EndLine"),
            "rule": item.get("RuleID", ""),
            "description": item.get("Description", ""),
            # Match/Secret are redacted by --redact=100 → show shape
            # so the model can confirm it's a real credential pattern
            # without seeing the underlying bytes.
            "match_redacted": item.get("Match", ""),
            "entropy": item.get("Entropy"),
            "tags": item.get("Tags") or [],
        })
        if len(findings) >= MAX_FINDINGS:
            break

    payload: dict[str, Any] = {"findings": findings, "count": len(findings)}
    if not findings:
        payload["note"] = "no hardcoded secrets detected"
    elif len(findings) >= MAX_FINDINGS:
        payload["truncated_at"] = MAX_FINDINGS
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
    "secrets_scan": secrets_scan,
    "lint_js": lint_js,
    "typecheck_ts": typecheck_ts,
    "lint_rust": lint_rust,
    "vet_go": vet_go,
}
