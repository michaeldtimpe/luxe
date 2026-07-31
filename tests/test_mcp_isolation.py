"""Tests for MCP client isolation — circuit breaker, caps, server-down propagation.

Real subprocess MCP servers are out of scope; these tests work directly on
MCPClientManager with mock _ServerRuntime objects. The integration boundary
is `sync_call`, which we exercise after pre-seeding state.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from luxe.mcp.client import (
    CircuitBreakerConfig,
    MCPClientConfig,
    MCPClientManager,
    MCPServerConfig,
    _ServerRuntime,
)


class _FakeContent:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeResult:
    def __init__(self, text: str, is_error: bool = False):
        self.isError = is_error
        self.content = [_FakeContent(text)]


class _FakeSession:
    """Minimal stub of mcp.ClientSession for tests."""
    def __init__(self, behavior: str = "ok", text: str = "fake-result"):
        self.behavior = behavior  # "ok" | "raise" | "timeout" | "error_result"
        self.text = text
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        if self.behavior == "ok":
            return _FakeResult(self.text)
        if self.behavior == "error_result":
            return _FakeResult("tool said no", is_error=True)
        if self.behavior == "raise":
            raise RuntimeError("server exploded")
        if self.behavior == "timeout":
            await asyncio.sleep(60)  # > timeout_s
        raise AssertionError("unknown behavior")

    async def list_tools(self):
        class _L:
            tools = []
        return _L()


def _bootstrap_manager_with_servers(servers: dict[str, _ServerRuntime]) -> MCPClientManager:
    """Build a manager that has its event loop running and the given servers
    pre-installed (skipping real MCP startup)."""
    cfg = MCPClientConfig(
        servers=[r.cfg for r in servers.values()],
        circuit_breaker=CircuitBreakerConfig(consecutive_failures=3, hard_cap_calls=200),
    )
    mgr = MCPClientManager(cfg)
    mgr._start_loop()
    mgr._servers = dict(servers)
    mgr._started = True
    return mgr


def _server(name: str, *, behavior: str, timeout_s: float = 1.0,
            max_calls: int = 50) -> _ServerRuntime:
    return _ServerRuntime(
        cfg=MCPServerConfig(name=name, command="x",
                            timeout_s=timeout_s, max_calls_per_session=max_calls),
        session=_FakeSession(behavior=behavior),
    )


# --- happy path -------------------------------------------------------------

def test_sync_call_returns_text_on_ok():
    mgr = _bootstrap_manager_with_servers({"a": _server("a", behavior="ok")})
    try:
        text, err = mgr.sync_call("a", "tool1", {"x": 1})
        assert err is None
        assert text == "fake-result"
    finally:
        mgr.close()


def test_sync_call_returns_error_on_isError():
    mgr = _bootstrap_manager_with_servers({"a": _server("a", behavior="error_result")})
    try:
        text, err = mgr.sync_call("a", "tool1", {})
        assert text == ""
        assert err is not None
        assert "tool said no" in err
    finally:
        mgr.close()


def test_sync_call_raise_records_failure():
    mgr = _bootstrap_manager_with_servers({"a": _server("a", behavior="raise")})
    try:
        text, err = mgr.sync_call("a", "tool1", {})
        assert err is not None
        assert "server exploded" in err
        assert mgr._servers["a"].consecutive_failures == 1
    finally:
        mgr.close()


# --- timeout ----------------------------------------------------------------

def test_sync_call_timeout_short_circuits():
    mgr = _bootstrap_manager_with_servers({"a": _server("a", behavior="timeout", timeout_s=0.1)})
    try:
        text, err = mgr.sync_call("a", "tool1", {})
        assert err is not None
        assert "timeout" in err.lower()
        assert mgr._servers["a"].consecutive_failures == 1
    finally:
        mgr.close()


# --- circuit breaker --------------------------------------------------------

def test_circuit_breaker_trips_after_3_failures():
    mgr = _bootstrap_manager_with_servers({"a": _server("a", behavior="raise")})
    try:
        for _ in range(3):
            mgr.sync_call("a", "tool1", {})
        assert mgr._servers["a"].is_down
        # Subsequent calls return immediately with the down message.
        text, err = mgr.sync_call("a", "tool1", {})
        assert err is not None
        assert "DOWN" in err
    finally:
        mgr.close()


def test_success_resets_consecutive_failures():
    runtime = _ServerRuntime(cfg=MCPServerConfig(name="a", command="x", timeout_s=1.0))
    runtime.session = _FakeSession(behavior="ok")
    mgr = _bootstrap_manager_with_servers({"a": runtime})
    try:
        # Force two failures by swapping to raise, then back to ok.
        mgr._servers["a"].session = _FakeSession(behavior="raise")
        mgr.sync_call("a", "x", {})
        mgr.sync_call("a", "x", {})
        assert mgr._servers["a"].consecutive_failures == 2
        mgr._servers["a"].session = _FakeSession(behavior="ok")
        text, err = mgr.sync_call("a", "x", {})
        assert err is None
        assert mgr._servers["a"].consecutive_failures == 0
    finally:
        mgr.close()


# --- caps -------------------------------------------------------------------

def test_per_server_max_calls_cap():
    mgr = _bootstrap_manager_with_servers({"a": _server("a", behavior="ok", max_calls=2)})
    try:
        mgr.sync_call("a", "x", {})
        mgr.sync_call("a", "x", {})
        text, err = mgr.sync_call("a", "x", {})
        assert err is not None
        assert "per-session cap" in err
    finally:
        mgr.close()


def test_global_hard_cap():
    mgr = _bootstrap_manager_with_servers({"a": _server("a", behavior="ok")})
    mgr.cfg.circuit_breaker.hard_cap_calls = 1
    try:
        mgr.sync_call("a", "x", {})
        text, err = mgr.sync_call("a", "x", {})
        assert err is not None
        assert "hard cap" in err
    finally:
        mgr.close()


# --- unknown server ---------------------------------------------------------

def test_unknown_server_returns_error():
    mgr = _bootstrap_manager_with_servers({})
    try:
        text, err = mgr.sync_call("nope", "x", {})
        assert err is not None
        assert "unknown" in err.lower()
    finally:
        mgr.close()


# --- server status ----------------------------------------------------------

def test_server_status_reports_down():
    mgr = _bootstrap_manager_with_servers({"a": _server("a", behavior="raise")})
    try:
        for _ in range(3):
            mgr.sync_call("a", "x", {})
        status = mgr.server_status()
        assert len(status) == 1
        assert status[0]["name"] == "a"
        assert status[0]["down"] is True
        assert status[0]["consecutive_failures"] == 3
    finally:
        mgr.close()


def test_server_status_empty_when_no_servers():
    mgr = _bootstrap_manager_with_servers({})
    try:
        assert mgr.server_status() == []
    finally:
        mgr.close()


# --- startup isolation ------------------------------------------------------
#
# Regression for the multi-server poisoning bug: every server used to share ONE
# AsyncExitStack in ONE task, so an unreachable server's anyio CancelledError
# (a BaseException, which `except Exception` misses) escaped and tore down the
# sessions of servers that had already connected. `--mcp alpha --mcp kappa`
# returned 0 tools while reporting both servers "up"; alpha alone returned 20.

def _http_server(name: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="streamable_http",
                           url=f"https://{name}.example/mcp", timeout_s=1.0)


def _manager_with_fake_connect(names: list[str], failures: dict[str, str],
                               monkeypatch) -> MCPClientManager:
    """Run the REAL start() path with the transport handshake faked out.

    `failures` maps a server name to how it dies:
      "cancel"     — bare anyio CancelledError, no cause attached
      "cancel+grp" — CancelledError, with the real cause raised on stack unwind
      "error"      — an ordinary Exception
    """
    async def _fake_connect_one(self, s, runtime, stack):
        mode = failures.get(s.name)
        if mode in ("cancel", "cancel+grp"):
            if mode == "cancel+grp":
                async def _raise_on_unwind(*exc_info):
                    raise ExceptionGroup(
                        "unhandled errors in a TaskGroup",
                        [ConnectionError("[SSL: CERTIFICATE_VERIFY_FAILED] nope")],
                    )
                stack.push_async_exit(_raise_on_unwind)
            raise asyncio.CancelledError("Cancelled via cancel scope 0xdeadbeef")
        if mode == "error":
            raise RuntimeError("plain boom")
        runtime.session = _FakeSession(behavior="ok")
        runtime.tool_names = [f"{s.name}_tool"]

    monkeypatch.setattr(MCPClientManager, "_async_connect_one", _fake_connect_one)
    cfg = MCPClientConfig(servers=[_http_server(n) for n in names],
                          circuit_breaker=CircuitBreakerConfig())
    return MCPClientManager(cfg).start()


def test_failing_server_does_not_poison_healthy_sibling(monkeypatch):
    mgr = _manager_with_fake_connect(["good", "bad"], {"bad": "cancel"}, monkeypatch)
    try:
        status = {s["name"]: s for s in mgr.server_status()}
        assert status["good"]["down"] is False
        assert mgr._servers["good"].session is not None
        assert status["bad"]["down"] is True
    finally:
        mgr.close()


def test_one_healthy_server_survives_several_failures(monkeypatch):
    mgr = _manager_with_fake_connect(
        ["bad1", "good", "bad2"],
        {"bad1": "cancel", "bad2": "error"},
        monkeypatch,
    )
    try:
        status = {s["name"]: s for s in mgr.server_status()}
        assert status["good"]["down"] is False
        assert status["bad1"]["down"] is True and status["bad2"]["down"] is True
        # The healthy server still lists tools — the whole point of the fix.
        defs, _fns = mgr.discover_tools()
        assert [d.name for d in defs] == []  # _FakeSession lists no tools
        assert mgr._servers["good"].consecutive_failures == 0
    finally:
        mgr.close()


def test_down_server_is_never_reported_up(monkeypatch):
    """A session-less runtime must never sit at down=False.

    That combination is what let the status line claim "0 tool(s) from 2
    server(s)" — counting servers as up that could not produce a single tool.
    """
    mgr = _manager_with_fake_connect(["bad"], {"bad": "cancel"}, monkeypatch)
    try:
        assert mgr._servers["bad"].session is None
        assert mgr._servers["bad"].is_down is True
        up = [s for s in mgr.server_status() if not s["down"]]
        assert up == []
    finally:
        mgr.close()


def test_down_reason_names_the_real_cause_not_the_cancellation(monkeypatch):
    """The cause surfaces on stack unwind; a bare cancel reason gets refined."""
    mgr = _manager_with_fake_connect(["bad"], {"bad": "cancel+grp"}, monkeypatch)
    try:
        reason = mgr._servers["bad"].down_reason
        assert "CERTIFICATE_VERIFY_FAILED" in reason
        assert "cancel scope" not in reason
    finally:
        mgr.close()


def test_discover_tools_skips_session_less_runtime():
    """Defence in depth: never dereference a None session (AttributeError was
    the second half of the original failure report)."""
    mgr = _bootstrap_manager_with_servers({"a": _server("a", behavior="ok")})
    try:
        mgr._servers["a"].session = None
        defs, fns = mgr.discover_tools()
        assert defs == [] and fns == {}
        assert mgr._servers["a"].consecutive_failures == 1
    finally:
        mgr.close()


def test_exc_text_flattens_exception_groups_and_names_bare_cancels():
    from luxe.mcp.client import _exc_text

    grp = ExceptionGroup("unhandled errors in a TaskGroup",
                         [ConnectionError("no route to host")])
    assert _exc_text(grp) == "no route to host"
    assert _exc_text(asyncio.CancelledError()) == "CancelledError"
