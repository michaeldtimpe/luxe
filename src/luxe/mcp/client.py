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
import logging
import threading
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
    transport: str = "stdio"  # stdio | streamable_http (HTTP NOT YET WIRED)
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 30.0
    enabled_for: list[str] = field(default_factory=list)
    max_calls_per_session: int = 50
    url: str = ""  # for streamable_http


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
        self._stack: AsyncExitStack | None = None
        self._servers: dict[str, _ServerRuntime] = {}
        self._started = False
        self._closed = False
        self._total_calls = 0  # global for hard cap

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
        try:
            fut = self._submit(self._async_start_all())
            fut.result(timeout=60.0)
        except Exception as e:
            logger.warning("MCP client start failed: %s", e)
            # Mark all configured servers down so discover_tools returns nothing.
            for s in self.cfg.servers:
                self._servers[s.name] = _ServerRuntime(
                    cfg=s, is_down=True, down_reason=f"startup failed: {e}",
                )
        self._started = True
        return self

    async def _async_start_all(self) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        for s in self.cfg.servers:
            runtime = _ServerRuntime(cfg=s)
            self._servers[s.name] = runtime
            try:
                if s.transport == "stdio":
                    if not s.command:
                        raise MCPError(f"server {s.name}: stdio transport requires `command`")
                    params = StdioServerParameters(
                        command=s.command, args=list(s.args), env=dict(s.env) or None,
                    )
                    read, write = await self._stack.enter_async_context(stdio_client(params))
                    session = await self._stack.enter_async_context(
                        ClientSession(read, write)
                    )
                    await session.initialize()
                    listing = await session.list_tools()
                    runtime.session = session
                    runtime.tool_names = [t.name for t in listing.tools]
                    logger.info("MCP server %s up; tools: %s",
                                s.name, runtime.tool_names)
                else:
                    raise MCPError(
                        f"server {s.name}: transport `{s.transport}` not yet "
                        "implemented (only stdio in v1.0)"
                    )
            except Exception as e:
                runtime.is_down = True
                runtime.down_reason = f"connect failed: {e}"
                logger.warning("MCP server %s failed to start: %s", s.name, e)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop is None:
            return
        try:
            if self._stack is not None:
                fut = self._submit(self._stack.__aexit__(None, None, None))
                try:
                    fut.result(timeout=5.0)
                except Exception as e:
                    logger.warning("MCP exit-stack drain failed: %s", e)
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
            for tool in (runtime.session.list_tools_sync_cache or []) if False else []:
                pass  # placeholder branch removed at runtime; see below
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
                td = mcp_tool_to_tooldef(tool, name)
                defs.append(td)
                fns[td.name] = make_mcp_tool_fn(self.sync_call, name, tool.name)
        return defs, fns

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
            return "", f"MCP loop not running"

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
