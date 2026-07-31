"""MCP client manager — runs MCP servers as subprocesses, exposes tools.

Architecture (per plan §4):
- One background asyncio thread owns the event loop where MCP ClientSessions
  live. This avoids forcing the rest of luxe to async.
- `sync_call(server, tool, args)` schedules an `asyncio.run_coroutine_threadsafe`
  call onto that loop and blocks until completion or per-call timeout.
- Per-server timeout (default 30s) wraps every call_tool.
- Circuit breaker: 3 consecutive timeouts/errors → server marked DOWN; its
  tools are reported via tooling but every subsequent call returns an error
  immediately (we don't re-route to a healthy server, since tools are unique).
- Soft + hard caps on calls per session per server.
- Subprocess lifetime: the manager owns the child processes via stdio_client's
  AsyncExitStack; `close()` cancels the loop and waits up to 5s before
  raising for any orphaned tasks.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from luxe.mcp.bridge import (
    make_mcp_tool_fn,
    mcp_tool_to_tooldef,
    render_mcp_call_result,
)
from luxe.tools.base import ToolDef, ToolFn

logger = logging.getLogger(__name__)


# --- config ----------------------------------------------------------------

@dataclass
class MCPServerConfig:
    name: str
    transport: str = "stdio"  # stdio | streamable_http
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 30.0
    enabled_for: list[str] = field(default_factory=list)
    max_calls_per_session: int = 50
    url: str = ""  # for streamable_http
    # Extra request headers for streamable_http (values may use ${VAR}).
    headers: dict[str, str] = field(default_factory=dict)
    # Name of the credential resolved via luxe.secrets (env → secrets.env →
    # keychain) and sent as `Authorization: Bearer <value>`. The VALUE never
    # appears in config — same rule as backends' api_key_env.
    api_key_env: str = ""
    # fnmatch patterns (raw tool names) that mutate remote state; interactive
    # chat only exposes them in write mode. Empty = nothing gated.
    gate_tools: list[str] = field(default_factory=list)
    # fnmatch allowlist (raw tool names); empty = expose every tool.
    only_tools: list[str] = field(default_factory=list)

    def tool_allowed(self, tool_name: str) -> bool:
        if not self.only_tools:
            return True
        return any(fnmatch.fnmatch(tool_name, p) for p in self.only_tools)

    def tool_gated(self, tool_name: str) -> bool:
        return any(fnmatch.fnmatch(tool_name, p) for p in self.gate_tools)


@dataclass
class CircuitBreakerConfig:
    consecutive_failures: int = 3
    hard_cap_calls: int = 200


@dataclass
class MCPClientConfig:
    servers: list[MCPServerConfig] = field(default_factory=list)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)


def default_mcp_config_path() -> Path:
    return Path(__file__).parent.parent.parent.parent / "configs" / "mcp.yaml"


def _interp_env(value: str) -> str:
    """Expand ${VAR} via os.environ; unset vars stay as-is (server may not need it)."""
    import os
    if not value or "${" not in value:
        return value
    out = value
    for key, env_val in os.environ.items():
        out = out.replace(f"${{{key}}}", env_val)
    return out


def load_mcp_config(path: str | Path | None = None) -> MCPClientConfig:
    p = Path(path) if path else default_mcp_config_path()
    if not p.is_file():
        return MCPClientConfig()
    raw = yaml.safe_load(p.read_text()) or {}
    client_raw = raw.get("client", {}) or {}
    servers = []
    for s in client_raw.get("servers", []) or []:
        servers.append(MCPServerConfig(
            name=str(s.get("name", "")),
            transport=str(s.get("transport", "stdio")),
            command=str(s.get("command", "")),
            args=[str(a) for a in s.get("args", [])],
            env={k: _interp_env(str(v)) for k, v in (s.get("env") or {}).items()},
            timeout_s=float(s.get("timeout_s", 30.0)),
            enabled_for=[str(x) for x in s.get("enabled_for", [])],
            max_calls_per_session=int(s.get("max_calls_per_session", 50)),
            url=str(s.get("url", "")),
            headers={k: _interp_env(str(v))
                     for k, v in (s.get("headers") or {}).items()},
            api_key_env=str(s.get("api_key_env", "")),
            gate_tools=[str(x) for x in s.get("gate_tools", []) or []],
            only_tools=[str(x) for x in s.get("only_tools", []) or []],
        ))
    cb_raw = client_raw.get("circuit_breaker", {}) or {}
    cb = CircuitBreakerConfig(
        consecutive_failures=int(cb_raw.get("consecutive_failures", 3)),
        hard_cap_calls=int(cb_raw.get("hard_cap_calls", 200)),
    )
    return MCPClientConfig(servers=servers, circuit_breaker=cb)


# --- exceptions ------------------------------------------------------------

class MCPError(RuntimeError):
    pass


def _exc_text(e: BaseException) -> str:
    """Readable one-liner for an exception, flattening ExceptionGroups.

    anyio surfaces transport failures as `ExceptionGroup('unhandled errors in
    a TaskGroup', [ConnectError(...)])`, whose `str()` hides the only useful
    part, and bare `CancelledError` stringifies to "". Both made `down_reason`
    useless — the reason a relay is down has to name the actual cause.
    """
    subs = getattr(e, "exceptions", None)
    if subs:
        return "; ".join(_exc_text(x) for x in subs)
    return str(e) or type(e).__name__


class ServerDown(MCPError):
    pass


class HardCapExceeded(MCPError):
    pass


# --- server runtime --------------------------------------------------------

@dataclass
class _ServerRuntime:
    cfg: MCPServerConfig
    session: Any = None        # mcp.ClientSession when up
    consecutive_failures: int = 0
    total_calls: int = 0
    is_down: bool = False
    down_reason: str = ""
    tool_names: list[str] = field(default_factory=list)


# --- manager ---------------------------------------------------------------

class MCPClientManager:
    """Connects to one or more MCP servers, exposes their tools to luxe.

    Lifecycle:
      mgr = MCPClientManager(cfg).start()
      tool_defs, tool_fns = mgr.discover_tools()
      ...inject into agent loop...
      mgr.close()  # at end of pipeline run
    """

    def __init__(self, cfg: MCPClientConfig):
        self.cfg = cfg
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._servers: dict[str, _ServerRuntime] = {}
        self._started = False
        self._closed = False
        self._total_calls = 0  # global for hard cap
        self._shutdown: asyncio.Event | None = None
        self._lifetime_fut = None

    # -- thread/loop bootstrap --

    def _start_loop(self) -> None:
        """Create a dedicated event loop in a background thread."""
        ready = threading.Event()

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            ready.set()
            self._loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_run, name="luxe-mcp-loop", daemon=True
        )
        self._loop_thread.start()
        ready.wait(timeout=5.0)
        if self._loop is None:
            raise MCPError("MCP event loop failed to start")

    def _submit(self, coro):
        """Schedule a coroutine on the manager's loop and return a Future."""
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # -- start / stop --

    def start(self) -> "MCPClientManager":
        if self._started:
            return self
        if not self.cfg.servers:
            self._started = True
            return self
        self._start_loop()
        readies = {s.name: threading.Event() for s in self.cfg.servers}
        self._lifetime_fut = self._submit(self._async_lifetime(readies))
        deadline = time.monotonic() + 120.0
        for ev in readies.values():
            ev.wait(timeout=max(0.0, deadline - time.monotonic()))
        exc = (self._lifetime_fut.exception()
               if self._lifetime_fut.done() else None)
        if exc is not None:
            logger.warning("MCP client start failed: %s", _exc_text(exc))
        # Honesty sweep: a server that never produced a session is DOWN, full
        # stop. Without this a runtime could sit at is_down=False with
        # session=None and be counted "up" while contributing zero tools —
        # exactly the state that made `--mcp a --mcp b` report
        # "0 tool(s) from 2 server(s)".
        for s in self.cfg.servers:
            runtime = self._servers.get(s.name)
            if runtime is None:
                self._servers[s.name] = _ServerRuntime(
                    cfg=s, is_down=True,
                    down_reason=f"startup never began: {_exc_text(exc)}"
                    if exc is not None else "startup never began",
                )
            elif runtime.session is None and not runtime.is_down:
                runtime.is_down = True
                runtime.down_reason = "startup timed out before the session was ready"
        self._started = True
        return self

    async def _async_lifetime(self, readies: dict[str, threading.Event]) -> None:
        """Supervise one INDEPENDENT connection task per server.

        Each server owns its own `AsyncExitStack` inside its own task. That is
        load-bearing twice over:

        1. anyio cancel scopes (streamablehttp_client, stdio_client) must be
           entered and exited on the same task, so the stack cannot be unwound
           from a `close()`-submitted coroutine.
        2. A failing transport raises out of its INTERNAL anyio task group as
           `CancelledError` — a BaseException. When every server shared one
           stack in one task, one unreachable server cancelled that scope and
           tore down the sessions of servers that had already connected fine:
           `--mcp alpha --mcp kappa` yielded 0 tools even though alpha alone
           yielded 20. Per-task stacks contain that blast radius.

        `return_exceptions=True` keeps one task's death from cancelling its
        siblings through the gather.
        """
        self._shutdown = asyncio.Event()
        try:
            await asyncio.gather(
                *(self._async_server_lifetime(s, readies[s.name])
                  for s in self.cfg.servers),
                return_exceptions=True,
            )
        finally:
            for ev in readies.values():
                ev.set()  # never leave start() hanging on a connect crash

    async def _async_server_lifetime(self, s: MCPServerConfig,
                                     ready: threading.Event) -> None:
        """Connect ONE server, signal `ready`, then hold it open until shutdown."""
        runtime = _ServerRuntime(cfg=s)
        self._servers[s.name] = runtime
        # A bare CancelledError names no cause ("Cancelled via cancel scope
        # 0x…"). The REAL error (ConnectError, SSL verify failure, …) only
        # surfaces as the ExceptionGroup thrown when the stack unwinds, so a
        # cancellation-derived reason is provisional and the unwind refines it.
        provisional_reason = False
        try:
            async with AsyncExitStack() as stack:
                try:
                    await self._async_connect_one(s, runtime, stack)
                except BaseException as e:  # noqa: BLE001 — see _async_lifetime
                    # BaseException, not Exception: anyio delivers a transport
                    # failure as CancelledError, which `except Exception` lets
                    # through. Letting it escape is what poisoned the siblings.
                    runtime.session = None
                    runtime.is_down = True
                    if isinstance(e, asyncio.CancelledError):
                        # Stay quiet: the unwind below names the real cause and
                        # logs it once. Reporting "transport cancelled" here too
                        # would print every failure twice, the useless line first.
                        provisional_reason = True
                        runtime.down_reason = "connect failed: transport cancelled"
                        logger.debug("MCP server %s cancelled during connect; "
                                     "awaiting the underlying cause", s.name)
                    else:
                        runtime.down_reason = f"connect failed: {_exc_text(e)}"
                        logger.warning("MCP server %s failed to start: %s",
                                       s.name, runtime.down_reason)
                    # Deliberately NOT signalling `ready` here: on the failure
                    # path the stack still has to unwind, and that unwind is
                    # what names the real cause. The outer `finally` signals
                    # once the reason is final, so start() never reads a
                    # provisional one.
                    return
                ready.set()
                assert self._shutdown is not None
                await self._shutdown.wait()
        except BaseException as e:  # noqa: BLE001
            # Raised while holding the connection open or while unwinding the
            # stack. During close() this is just teardown noise; before it, the
            # session is genuinely gone.
            runtime.session = None
            if provisional_reason:
                runtime.down_reason = f"connect failed: {_exc_text(e)}"
                logger.warning("MCP server %s failed to start: %s",
                               s.name, _exc_text(e))
            elif not (self._closed or runtime.is_down):
                runtime.is_down = True
                runtime.down_reason = f"connection lost: {_exc_text(e)}"
                logger.warning("MCP server %s connection lost: %s",
                               s.name, _exc_text(e))
            else:
                # Teardown noise from close() — the exit stacks unwinding is
                # expected, not a fault worth a warning during shutdown.
                logger.debug("MCP server %s lifetime ended: %s", s.name, _exc_text(e))
        finally:
            ready.set()

    async def _async_connect_one(self, s: MCPServerConfig,
                                 runtime: _ServerRuntime,
                                 stack: AsyncExitStack) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        if s.transport == "stdio":
            if not s.command:
                raise MCPError(f"server {s.name}: stdio transport requires `command`")
            params = StdioServerParameters(
                command=s.command, args=list(s.args), env=dict(s.env) or None,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        elif s.transport == "streamable_http":
            if not s.url:
                raise MCPError(
                    f"server {s.name}: streamable_http transport requires `url`")
            from mcp.client.streamable_http import streamablehttp_client
            headers = dict(s.headers)
            if s.api_key_env:
                from luxe.secrets import resolve_api_key
                token = resolve_api_key(s.api_key_env)
                if not token:
                    raise MCPError(
                        f"server {s.name}: no credential for "
                        f"{s.api_key_env} (env → ~/.luxe/secrets.env → "
                        "keychain)")
                headers.setdefault("Authorization", f"Bearer {token}")
            read, write, _get_sid = await stack.enter_async_context(
                streamablehttp_client(s.url, headers=headers or None)
            )
        else:
            raise MCPError(
                f"server {s.name}: unknown transport `{s.transport}` "
                "(stdio | streamable_http)"
            )
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listing = await session.list_tools()
        runtime.session = session
        runtime.tool_names = [t.name for t in listing.tools]
        logger.info("MCP server %s up; tools: %s", s.name, runtime.tool_names)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop is None:
            return
        try:
            # Ask the lifetime task to unwind its own exit stack (same-task
            # rule for anyio cancel scopes), then wait for it to finish.
            if self._shutdown is not None:
                self._loop.call_soon_threadsafe(self._shutdown.set)
            if self._lifetime_fut is not None:
                try:
                    self._lifetime_fut.result(timeout=10.0)
                except Exception as e:
                    logger.warning("MCP lifetime drain failed: %s", e)
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._loop_thread:
                self._loop_thread.join(timeout=5.0)
        except Exception as e:
            logger.warning("MCP close error: %s", e)

    def __enter__(self) -> "MCPClientManager":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- discovery / dispatch --

    def discover_tools(self, *, only_for_task: str | None = None,
                       ) -> tuple[list[ToolDef], dict[str, ToolFn]]:
        defs: list[ToolDef] = []
        fns: dict[str, ToolFn] = {}
        for name, runtime in self._servers.items():
            if runtime.is_down:
                continue
            if only_for_task and runtime.cfg.enabled_for and \
                    only_for_task not in runtime.cfg.enabled_for:
                continue
            if runtime.session is None:
                # Belt-and-braces against the start() sweep ever missing one:
                # never dereference a session-less runtime (that AttributeError
                # was the second half of the "0 tools from 2 servers" report).
                self._record_failure(runtime, "no session")
                continue
            # We hold the tool listing on _ServerRuntime.tool_names; re-fetch
            # via async call to get full Tool objects with schemas.
            try:
                fut = self._submit(runtime.session.list_tools())
                listing = fut.result(timeout=runtime.cfg.timeout_s)
            except Exception as e:
                logger.warning("MCP %s list_tools failed: %s", name, e)
                self._record_failure(runtime, str(e))
                continue
            for tool in listing.tools:
                if not runtime.cfg.tool_allowed(tool.name):
                    continue
                td = mcp_tool_to_tooldef(tool, name)
                defs.append(td)
                fns[td.name] = make_mcp_tool_fn(self.sync_call, name, tool.name)
        return defs, fns

    def is_write_gated(self, namespaced_name: str) -> bool:
        """True when this `mcp__server__tool` def matches its server's
        `gate_tools` patterns — i.e. it mutates remote state and interactive
        chat should withhold it until write mode is on."""
        from luxe.mcp.bridge import split_namespaced_name
        parts = split_namespaced_name(namespaced_name)
        if parts is None:
            return False
        server, tool = parts
        runtime = self._servers.get(server)
        if runtime is None:
            return False
        return runtime.cfg.tool_gated(tool)

    def _record_failure(self, runtime: _ServerRuntime, reason: str) -> None:
        runtime.consecutive_failures += 1
        if runtime.consecutive_failures >= self.cfg.circuit_breaker.consecutive_failures:
            runtime.is_down = True
            runtime.down_reason = (
                f"circuit-breaker tripped after {runtime.consecutive_failures} "
                f"consecutive failures: {reason}"
            )
            logger.warning("MCP server %s tripped circuit breaker: %s",
                           runtime.cfg.name, reason)

    def _record_success(self, runtime: _ServerRuntime) -> None:
        runtime.consecutive_failures = 0

    # -- the workhorse --

    def sync_call(self, server_name: str, tool_name: str,
                  args: dict[str, Any]) -> tuple[str, str | None]:
        runtime = self._servers.get(server_name)
        if runtime is None:
            return "", f"unknown MCP server: {server_name}"
        if runtime.is_down:
            return "", f"MCP server `{server_name}` is DOWN: {runtime.down_reason}"

        if self._total_calls >= self.cfg.circuit_breaker.hard_cap_calls:
            return "", (
                f"MCP hard cap reached "
                f"({self.cfg.circuit_breaker.hard_cap_calls} calls); "
                "all servers refusing further calls for this run"
            )
        if runtime.total_calls >= runtime.cfg.max_calls_per_session:
            return "", (
                f"MCP server `{server_name}` per-session cap reached "
                f"({runtime.cfg.max_calls_per_session} calls)"
            )

        if self._loop is None:
            return "", "MCP loop not running"

        async def _do():
            return await asyncio.wait_for(
                runtime.session.call_tool(tool_name, args),
                timeout=runtime.cfg.timeout_s,
            )

        try:
            fut = self._submit(_do())
            result = fut.result(timeout=runtime.cfg.timeout_s + 5.0)
        except asyncio.TimeoutError:
            self._record_failure(runtime, f"timeout after {runtime.cfg.timeout_s}s")
            self._total_calls += 1
            runtime.total_calls += 1
            return "", f"MCP call_tool timeout after {runtime.cfg.timeout_s}s"
        except Exception as e:
            self._record_failure(runtime, f"{type(e).__name__}: {e}")
            self._total_calls += 1
            runtime.total_calls += 1
            return "", f"MCP call_tool error: {type(e).__name__}: {e}"

        self._record_success(runtime)
        self._total_calls += 1
        runtime.total_calls += 1

        is_error = bool(getattr(result, "isError", False))
        text = render_mcp_call_result(getattr(result, "content", []) or [])
        if is_error:
            return "", text or "MCP tool reported isError=true"
        return text, None

    def server_status(self) -> list[dict[str, Any]]:
        out = []
        for name, runtime in self._servers.items():
            out.append({
                "name": name,
                "down": runtime.is_down,
                "down_reason": runtime.down_reason,
                "consecutive_failures": runtime.consecutive_failures,
                "total_calls": runtime.total_calls,
                "tool_count": len(runtime.tool_names),
            })
        return out
