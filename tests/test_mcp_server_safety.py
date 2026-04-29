"""Tests for src/luxe/mcp/server.py — read-only-by-default + token gate."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import luxe.mcp.server as srv
from luxe.mcp.server import (
    RateLimiter,
    ServerPolicy,
    _check_confirm_token,
    _parse_rate,
    _redact_args,
    _unsafe_enabled,
    build_server,
    load_server_policy,
    make_read_only_role,
    server_tool_names,
)


def audit_log_path():
    """Read via the (possibly monkey-patched) module attribute."""
    return srv.audit_log_path()


@pytest.fixture(autouse=True)
def _isolate_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr("luxe.mcp.server.audit_log_path",
                        lambda: tmp_path / "mcp_audit.jsonl")


def _read_audit(p_path: Path) -> list[dict]:
    p = audit_log_path()
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# --- read-only role patcher --------------------------------------------------

def test_make_read_only_role_strips_mutation_tools():
    from luxe.config import RoleConfig
    role = RoleConfig(model_key="x", num_ctx=1024, max_steps=1,
                      tools=["read_file", "write_file", "edit_file", "bash", "grep"])
    new = make_read_only_role(role)
    assert "write_file" not in new.tools
    assert "edit_file" not in new.tools
    assert "bash" not in new.tools
    assert "read_file" in new.tools
    assert "grep" in new.tools


def test_make_read_only_role_handles_empty_tools():
    from luxe.config import RoleConfig
    role = RoleConfig(model_key="x", num_ctx=1024, max_steps=1, tools=[])
    new = make_read_only_role(role)
    assert new.tools == []


# --- unsafe gate -----------------------------------------------------------

def test_unsafe_disabled_when_env_unset(monkeypatch):
    monkeypatch.delenv("LUXE_MCP_UNSAFE", raising=False)
    assert not _unsafe_enabled()


def test_unsafe_enabled_only_when_env_eq_1(monkeypatch):
    monkeypatch.setenv("LUXE_MCP_UNSAFE", "1")
    assert _unsafe_enabled()
    monkeypatch.setenv("LUXE_MCP_UNSAFE", "yes")
    assert not _unsafe_enabled()


def test_confirm_token_unset_env(monkeypatch):
    monkeypatch.delenv("LUXE_MCP_TOKEN", raising=False)
    ok, msg = _check_confirm_token("anything")
    assert not ok
    assert "LUXE_MCP_TOKEN" in msg


def test_confirm_token_match(monkeypatch):
    monkeypatch.setenv("LUXE_MCP_TOKEN", "secret-xyz")
    ok, msg = _check_confirm_token("secret-xyz")
    assert ok


def test_confirm_token_mismatch(monkeypatch):
    monkeypatch.setenv("LUXE_MCP_TOKEN", "secret-xyz")
    ok, msg = _check_confirm_token("wrong")
    assert not ok
    assert "does not match" in msg


# --- redaction --------------------------------------------------------------

def test_redact_args_strips_secret_keys():
    out = _redact_args({"github_token": "abc", "title": "fix bug",
                        "api_key": "xyz", "password": "pw"})
    assert out["github_token"] == "[redacted]"
    assert out["api_key"] == "[redacted]"
    assert out["password"] == "[redacted]"
    assert out["title"] == "fix bug"


def test_redact_args_truncates_long_strings():
    out = _redact_args({"prompt": "a" * 500})
    assert out["prompt"].endswith("(truncated)")
    assert len(out["prompt"]) < 500


# --- rate limiter ---------------------------------------------------------

def test_parse_rate():
    assert _parse_rate("1/min") == (1, 60.0)
    assert _parse_rate("6/min") == (6, 60.0)
    assert _parse_rate("10/sec") == (10, 1.0)
    assert _parse_rate("100/hour") == (100, 3600.0)


def test_rate_limiter_within_window():
    rl = RateLimiter()
    rl.configure({"luxe_review": "3/min"})
    for _ in range(3):
        ok, _ = rl.check("luxe_review")
        assert ok
    ok, msg = rl.check("luxe_review")
    assert not ok
    assert "rate limit" in msg


def test_rate_limiter_unlimited_when_no_rule():
    rl = RateLimiter()
    for _ in range(50):
        ok, _ = rl.check("anything")
        assert ok


# --- server build & tool listing ------------------------------------------

def test_server_tool_names_readonly_default():
    names = server_tool_names(unsafe=False)
    assert "luxe_review" in names
    assert "luxe_summarize" in names
    assert "luxe_explain" in names
    assert "luxe_maintain" not in names


def test_server_tool_names_unsafe_adds_maintain():
    names = server_tool_names(unsafe=True)
    assert "luxe_maintain" in names


def test_build_server_readonly_skips_maintain_tool():
    server = build_server(unsafe=False)
    tool_names = [t.name for t in server._tool_manager.list_tools()]
    assert "luxe_review" in tool_names
    assert "luxe_summarize" in tool_names
    assert "luxe_explain" in tool_names
    assert "luxe_maintain" not in tool_names


def test_build_server_unsafe_registers_maintain():
    server = build_server(unsafe=True)
    tool_names = [t.name for t in server._tool_manager.list_tools()]
    assert "luxe_maintain" in tool_names


# --- runner integration -----------------------------------------------------

def test_readonly_runner_called(monkeypatch):
    monkeypatch.setenv("LUXE_MCP_TOKEN", "secret")
    captured: list[tuple[str, dict]] = []

    def runner(name, args):
        captured.append((name, args))
        return "review report"

    server = build_server(unsafe=False, readonly_runner=runner)
    # Find the registered review tool and invoke its underlying fn directly.
    tools = {t.name: t for t in server._tool_manager.list_tools()}
    out = server._tool_manager._tools["luxe_review"].fn(  # type: ignore[attr-defined]
        repo_path="/r", goal="g")
    assert out == "review report"
    assert captured == [("luxe_review", {"repo_path": "/r", "goal": "g"})]


def test_maintain_runner_rejected_without_unsafe_env(monkeypatch):
    """Even with unsafe=True at build time, runtime requires the env var."""
    monkeypatch.delenv("LUXE_MCP_UNSAFE", raising=False)

    def runner(args):
        raise AssertionError("must not be called")

    server = build_server(unsafe=True, maintain_runner=runner)
    out = server._tool_manager._tools["luxe_maintain"].fn(  # type: ignore[attr-defined]
        repo_path="/r", goal="g", confirm_token="x")
    assert "not enabled" in out
    audit = _read_audit(audit_log_path())
    assert audit and audit[-1]["outcome"] == "rejected"


def test_maintain_runner_rejected_on_token_mismatch(monkeypatch):
    monkeypatch.setenv("LUXE_MCP_UNSAFE", "1")
    monkeypatch.setenv("LUXE_MCP_TOKEN", "right")

    def runner(args):
        raise AssertionError("must not be called")

    server = build_server(unsafe=True, maintain_runner=runner)
    out = server._tool_manager._tools["luxe_maintain"].fn(  # type: ignore[attr-defined]
        repo_path="/r", goal="g", confirm_token="wrong")
    assert "rejected" in out
    audit = _read_audit(audit_log_path())
    assert audit and audit[-1]["outcome"] == "rejected"


def test_maintain_runner_called_with_correct_token(monkeypatch):
    monkeypatch.setenv("LUXE_MCP_UNSAFE", "1")
    monkeypatch.setenv("LUXE_MCP_TOKEN", "right")
    called = {"n": 0}

    def runner(args):
        called["n"] += 1
        return "maintained"

    server = build_server(unsafe=True, maintain_runner=runner)
    out = server._tool_manager._tools["luxe_maintain"].fn(  # type: ignore[attr-defined]
        repo_path="/r", goal="g", confirm_token="right")
    assert called["n"] == 1
    assert out == "maintained"


# --- audit log -------------------------------------------------------------

def test_audit_log_redacts_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("LUXE_MCP_UNSAFE", "1")
    monkeypatch.setenv("LUXE_MCP_TOKEN", "right")

    def runner(args):
        return "ok"

    server = build_server(unsafe=True, maintain_runner=runner)
    server._tool_manager._tools["luxe_maintain"].fn(  # type: ignore[attr-defined]
        repo_path="/r", goal="g", confirm_token="right")

    audit = _read_audit(audit_log_path())
    assert audit
    last = audit[-1]
    assert last["args"]["confirm_token"] == "[redacted]"
    assert last["outcome"] == "accepted"
