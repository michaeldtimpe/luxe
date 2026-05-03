"""CVE lookup tool — queries the OSV.dev advisory database for known
vulnerabilities affecting a package version.

Closes the audit-hallucination caveat from v1.1's deps-audit (see
`project_tool_subphases_and_cve_lookup.md`). Before this tool, the
`manage_strict` overlay pushed the model to produce CVE-shaped findings
in the right structural shape, but the bench's regex grader couldn't
distinguish a real CVE id from a plausible-but-invented one. With this
tool the model can ground its findings in deterministic upstream data.

Backed by https://api.osv.dev/v1/query — free, public, no auth, multi-
ecosystem (PyPI, npm, Go, Maven, RubyGems, crates.io, NuGet, ...).
Responses are cached to ~/.luxe/cve_cache/ with 24h TTL so repeat
lookups within a multi-fixture bench run don't pound the API.

Response is compressed before returning to the model: only id, summary
(truncated), severity score, fixed versions, and the top advisory URL
survive. The full OSV.dev response includes long prose 'details'
blocks and ecosystem-specific chaff that would otherwise burn prompt
tokens with no benefit.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from luxe.tools.base import ToolDef, ToolFn

_OSV_API = "https://api.osv.dev/v1/query"
_CACHE_DIR = Path.home() / ".luxe" / "cve_cache"
_CACHE_TTL_SEC = 24 * 3600  # 24 hours
_HTTP_TIMEOUT = 10.0
_MAX_VULNS_PER_RESPONSE = 30   # truncation cap; keeps token cost bounded
_SUMMARY_MAX_CHARS = 400        # truncation cap on each vuln's summary text


def _cache_key(package: str, ecosystem: str, version: str | None) -> Path:
    """Filesystem-safe cache key. Versions like '3.9.0,<4.0.0' get sanitized."""
    safe_version = (version or "any").replace("/", "_").replace(":", "_")
    safe_pkg = package.replace("/", "_")
    return _CACHE_DIR / f"{ecosystem.lower()}__{safe_pkg}__{safe_version}.json"


def _read_cache(p: Path) -> dict[str, Any] | None:
    if not p.is_file():
        return None
    try:
        mtime = p.stat().st_mtime
        if time.time() - mtime > _CACHE_TTL_SEC:
            return None
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(p: Path, data: dict[str, Any]) -> None:
    """Best-effort write — we never want a cache write to fail the lookup."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
    except OSError:
        pass


def _query_osv(package: str, ecosystem: str, version: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"package": {"name": package, "ecosystem": ecosystem}}
    if version:
        payload["version"] = version
    r = httpx.post(_OSV_API, json=payload, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _compress_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Prune OSV.dev's verbose response down to fields the model needs.

    Drops 'details' prose, ecosystem-specific 'database_specific' blocks,
    and most reference URLs. Keeps id, summary (truncated), severity
    score, fixed-version list, and top advisory URL — enough to write a
    real audit finding without burning thousands of tokens.
    """
    vulns = raw.get("vulns") or []
    out: list[dict[str, Any]] = []
    for v in vulns[:_MAX_VULNS_PER_RESPONSE]:
        compressed: dict[str, Any] = {
            "id": v.get("id", ""),
            "summary": (v.get("summary") or "")[:_SUMMARY_MAX_CHARS],
        }
        # Severity — OSV.dev gives a list; first entry is typically the
        # primary CVSS score.
        sev_list = v.get("severity") or []
        if sev_list and isinstance(sev_list[0], dict):
            compressed["severity"] = sev_list[0].get("score", "")
        # Fixed versions across all 'affected' entries — these are the
        # upgrade targets a real audit needs to recommend.
        fixed: list[str] = []
        for aff in v.get("affected") or []:
            for r in aff.get("ranges") or []:
                for ev in r.get("events") or []:
                    if "fixed" in ev:
                        fixed.append(ev["fixed"])
        compressed["fixed_versions"] = sorted(set(fixed))
        # Top advisory URL — prefer ADVISORY-typed refs; fall back to
        # WEB-typed; then any. Avoids returning every commit/issue link.
        refs = v.get("references") or []
        url = ""
        for ref in refs:
            if ref.get("type") == "ADVISORY":
                url = ref.get("url", "")
                if url:
                    break
        if not url:
            for ref in refs:
                if ref.get("type") == "WEB":
                    url = ref.get("url", "")
                    if url:
                        break
        compressed["advisory_url"] = url
        out.append(compressed)
    return {
        "count": len(out),
        "truncated": len(vulns) > _MAX_VULNS_PER_RESPONSE,
        "vulnerabilities": out,
    }


def cve_lookup_fn(args: dict[str, Any]) -> tuple[str, str | None]:
    package = args.get("package", "").strip()
    ecosystem = args.get("ecosystem", "PyPI").strip() or "PyPI"
    version_arg = args.get("version")
    version: str | None = None
    if version_arg is not None:
        s = str(version_arg).strip()
        if s:
            version = s
    if not package:
        return "", "package is required"

    cache_path = _cache_key(package, ecosystem, version)
    cached = _read_cache(cache_path)
    if cached is not None:
        return json.dumps(cached, indent=2), None

    try:
        raw = _query_osv(package, ecosystem, version)
    except httpx.HTTPError as e:
        return "", f"OSV.dev query failed: {e}"
    except Exception as e:
        return "", f"OSV.dev unexpected error: {type(e).__name__}: {e}"

    compressed = _compress_response(raw)
    _write_cache(cache_path, compressed)
    return json.dumps(compressed, indent=2), None


def cve_lookup_def() -> ToolDef:
    return ToolDef(
        name="cve_lookup",
        description=(
            "Look up known security vulnerabilities for a package via the "
            "OSV.dev advisory database. Use this BEFORE citing any CVE id "
            "in a security audit — the bench grader checks shape, not "
            "factuality, but real-world auditors check both. Returns up "
            "to 30 vulnerabilities, each with id, summary, severity, fixed "
            "versions, and an advisory URL. Supports PyPI, npm, Go, Maven, "
            "RubyGems, crates.io, NuGet, and others."
        ),
        parameters={
            "type": "object",
            "properties": {
                "package": {
                    "type": "string",
                    "description": "Package name (e.g. 'aiohttp', 'lodash').",
                },
                "ecosystem": {
                    "type": "string",
                    "description": (
                        "Package ecosystem; default 'PyPI'. Other values: "
                        "'npm', 'Go', 'Maven', 'RubyGems', 'crates.io', "
                        "'NuGet'."
                    ),
                },
                "version": {
                    "type": "string",
                    "description": (
                        "Optional specific version (e.g. '3.9.0'). When set, "
                        "OSV.dev filters to vulnerabilities that affect this "
                        "version. When omitted, returns all known "
                        "vulnerabilities for the package."
                    ),
                },
            },
            "required": ["package"],
        },
    )


TOOL_FNS: dict[str, ToolFn] = {"cve_lookup": cve_lookup_fn}
CACHEABLE: set[str] = {"cve_lookup"}
