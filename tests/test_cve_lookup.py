"""Tests for src/luxe/tools/cve_lookup.py — OSV.dev-backed CVE lookup tool."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from luxe.tools import cve_lookup


# Realistic-shaped OSV.dev response excerpt for aiohttp's known DoS CVE.
# Real response is much longer; this is enough to exercise compression.
_OSV_AIOHTTP_RESPONSE = {
    "vulns": [
        {
            "id": "GHSA-q3qx-c6g2-7pw2",
            "aliases": ["CVE-2023-46136", "PYSEC-2023-225"],
            "summary": "aiohttp 3.9.0 and earlier have a DoS vulnerability in body parser",
            "details": "A long prose details block we don't want in the model's prompt — it eats tokens with no audit value.",
            "severity": [{"type": "CVSS_V3", "score": "7.5"}],
            "affected": [
                {
                    "package": {"name": "aiohttp", "ecosystem": "PyPI"},
                    "ranges": [
                        {
                            "type": "ECOSYSTEM",
                            "events": [
                                {"introduced": "0"},
                                {"fixed": "3.9.4"},
                            ],
                        },
                    ],
                },
            ],
            "references": [
                {"type": "ADVISORY", "url": "https://github.com/aio-libs/aiohttp/security/advisories/GHSA-q3qx-c6g2-7pw2"},
                {"type": "WEB", "url": "https://docs.aiohttp.org/en/stable/changes.html"},
                {"type": "REPORT", "url": "https://github.com/aio-libs/aiohttp/issues/12345"},
            ],
            "database_specific": {"github_reviewed": True, "cwe_ids": ["CWE-770"]},
        },
    ],
}


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    """Redirect the cache to a per-test tmp dir so tests don't interfere with
    the user's real ~/.luxe/cve_cache or with each other."""
    monkeypatch.setattr(cve_lookup, "_CACHE_DIR", tmp_path / "cve_cache")
    yield


# --- compression --

def test_compress_response_keeps_load_bearing_fields():
    """The compressed response must keep id, aliases, summary (truncated),
    severity, fixed_versions, and a top advisory URL — the fields a real
    audit needs to ground its findings without inventing ids."""
    out = cve_lookup._compress_response(_OSV_AIOHTTP_RESPONSE)
    assert out["count"] == 1
    assert out["truncated"] is False
    v = out["vulnerabilities"][0]
    assert v["id"] == "GHSA-q3qx-c6g2-7pw2"
    # Aliases are load-bearing — many advisories have OSV's id as a GHSA
    # but the model + downstream readers expect the CVE id. Without
    # aliases preserved, the model would have to recall the CVE from
    # training data (sometimes correct, never deterministic).
    assert v["aliases"] == ["CVE-2023-46136", "PYSEC-2023-225"]
    assert "DoS" in v["summary"]
    assert v["severity"] == "7.5"
    assert v["fixed_versions"] == ["3.9.4"]
    # Prefer ADVISORY-typed reference; not the WEB-typed or REPORT.
    assert "GHSA-q3qx-c6g2-7pw2" in v["advisory_url"]


def test_compress_response_handles_missing_aliases():
    """Some OSV entries have no aliases (e.g. PYSEC-only or GHSA-only).
    The compressor must return an empty list, not crash, not omit the key.
    Stable schema makes the model's prompt parsing predictable."""
    raw = {"vulns": [{"id": "PYSEC-2019-123", "summary": "x",
                      "affected": [], "references": []}]}
    out = cve_lookup._compress_response(raw)
    assert out["vulnerabilities"][0]["aliases"] == []


def test_compress_response_drops_prose_details():
    """The OSV.dev `details` field is verbose prose. The compressor should
    NOT include it — burns prompt tokens for no audit value."""
    out = cve_lookup._compress_response(_OSV_AIOHTTP_RESPONSE)
    v = out["vulnerabilities"][0]
    # 'details' should not be in the compressed output.
    assert "details" not in v
    # 'database_specific' should not be in the compressed output.
    assert "database_specific" not in v


def test_compress_response_truncates_summary():
    """Summary truncated to _SUMMARY_MAX_CHARS (400). Defends against rare
    OSV entries with very long summary fields."""
    long_summary = "x" * 1000
    raw = {"vulns": [{"id": "CVE-1", "summary": long_summary,
                      "affected": [], "references": []}]}
    out = cve_lookup._compress_response(raw)
    assert len(out["vulnerabilities"][0]["summary"]) == cve_lookup._SUMMARY_MAX_CHARS


def test_compress_response_truncation_flag_set_when_over_limit():
    """When OSV returns more than _MAX_VULNS_PER_RESPONSE entries, the
    compressed response must flag truncation so the model knows it isn't
    seeing the full list."""
    raw = {"vulns": [{"id": f"CVE-{i}", "summary": "", "affected": [],
                      "references": []}
                     for i in range(cve_lookup._MAX_VULNS_PER_RESPONSE + 5)]}
    out = cve_lookup._compress_response(raw)
    assert out["count"] == cve_lookup._MAX_VULNS_PER_RESPONSE
    assert out["truncated"] is True


def test_compress_response_advisory_falls_back_to_web():
    """If no ADVISORY-typed reference exists, fall back to the first WEB-
    typed one. Prefer NOT to surface REPORT-typed (issue/PR) refs since
    those are noise for an auditor."""
    raw = {"vulns": [{
        "id": "CVE-X",
        "summary": "",
        "affected": [],
        "references": [
            {"type": "REPORT", "url": "https://github.com/x/y/issues/1"},
            {"type": "WEB", "url": "https://docs.example.com/security"},
        ],
    }]}
    out = cve_lookup._compress_response(raw)
    assert "docs.example.com" in out["vulnerabilities"][0]["advisory_url"]


# --- caching --

def test_cache_roundtrip(tmp_path):
    """Write then read; confirm equality."""
    p = tmp_path / "cache.json"
    cve_lookup._write_cache(p, {"hello": "world"})
    assert cve_lookup._read_cache(p) == {"hello": "world"}


def test_cache_miss_on_stale_entry(tmp_path):
    """Entries older than _CACHE_TTL_SEC return None (forces re-query)."""
    p = tmp_path / "cache.json"
    cve_lookup._write_cache(p, {"x": 1})
    # Backdate the file to before TTL.
    old = time.time() - cve_lookup._CACHE_TTL_SEC - 60
    import os
    os.utime(p, (old, old))
    assert cve_lookup._read_cache(p) is None


def test_cache_miss_on_missing_file(tmp_path):
    assert cve_lookup._read_cache(tmp_path / "doesnotexist.json") is None


# --- top-level fn --

def test_cve_lookup_fn_requires_package():
    result, err = cve_lookup.cve_lookup_fn({})
    assert result == ""
    assert "package is required" in (err or "")


def test_cve_lookup_fn_uses_cache_on_repeat_call():
    """Two calls with identical args should hit the network ONCE — the
    second call resolves from the cached response."""
    args = {"package": "aiohttp", "version": "3.9.0"}
    with patch.object(cve_lookup, "_query_osv",
                      return_value=_OSV_AIOHTTP_RESPONSE) as mock_q:
        r1, e1 = cve_lookup.cve_lookup_fn(args)
        r2, e2 = cve_lookup.cve_lookup_fn(args)
    assert e1 is None and e2 is None
    assert r1 == r2  # same compressed response
    assert mock_q.call_count == 1  # second call hit cache, not network


def test_cve_lookup_fn_returns_compressed_json():
    """Response is JSON the model can parse; canonical id (GHSA) surfaces
    plus aliases (containing the CVE) and fixed_versions."""
    with patch.object(cve_lookup, "_query_osv",
                      return_value=_OSV_AIOHTTP_RESPONSE):
        result, err = cve_lookup.cve_lookup_fn(
            {"package": "aiohttp", "version": "3.9.0"})
    assert err is None
    parsed = json.loads(result)
    assert parsed["count"] == 1
    v = parsed["vulnerabilities"][0]
    assert v["id"] == "GHSA-q3qx-c6g2-7pw2"
    assert "CVE-2023-46136" in v["aliases"]
    assert v["fixed_versions"] == ["3.9.4"]


def test_cve_lookup_fn_handles_http_error_cleanly():
    """Network failures return a clean error message rather than crashing.
    The agent loop surfaces the error to the model so it can either retry
    or move on; an unhandled exception would escape dispatch_tool."""
    err_to_raise = httpx.ConnectError("name resolution failed")
    with patch.object(cve_lookup, "_query_osv", side_effect=err_to_raise):
        result, err = cve_lookup.cve_lookup_fn({"package": "aiohttp"})
    assert result == ""
    assert err is not None
    assert "OSV.dev" in err


def test_cve_lookup_fn_passes_ecosystem_through():
    """Ecosystem defaults to PyPI but should be configurable for npm, etc.
    Defends against accidental hardcoding to PyPI."""
    captured: dict[str, object] = {}

    def fake_query(pkg, eco, ver):
        captured["pkg"] = pkg
        captured["eco"] = eco
        captured["ver"] = ver
        return {"vulns": []}

    with patch.object(cve_lookup, "_query_osv", side_effect=fake_query):
        cve_lookup.cve_lookup_fn(
            {"package": "lodash", "ecosystem": "npm", "version": "4.17.20"})
    assert captured == {"pkg": "lodash", "eco": "npm", "ver": "4.17.20"}


# --- tool def schema --

def test_cve_lookup_def_has_required_package():
    """The tool def's JSON schema must mark `package` as required so
    validate_args rejects a call without it before dispatching."""
    d = cve_lookup.cve_lookup_def()
    assert d.name == "cve_lookup"
    assert "package" in d.parameters["required"]


def test_cve_lookup_def_documents_use_before_citing():
    """The tool description must tell the model to look up CVEs BEFORE
    citing them — that's the whole point. If the description is fuzzy
    on this, the model will skip the tool and hallucinate as before."""
    d = cve_lookup.cve_lookup_def()
    assert "BEFORE" in d.description.upper()
