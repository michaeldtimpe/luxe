"""MCP-in-chat regression tests (chat.sdd § MCP tools in chat).

Covers: streamable_http config parsing (headers/api_key_env/gate_tools/
only_tools), the fnmatch gate/allow predicates, manager-level write-gate
resolution for namespaced defs, the chat/mcptools registry, and the rule that
a bad transport marks the server DOWN instead of raising (an unreachable
relay must not stop a chat session from starting).
"""

from __future__ import annotations

import textwrap

from luxe.chat import mcptools
from luxe.mcp.client import (
    MCPClientConfig,
    MCPClientManager,
    MCPServerConfig,
    _ServerRuntime,
    load_mcp_config,
)
from luxe.tools.base import ToolDef


def _write_cfg(tmp_path, body: str):
    p = tmp_path / "mcp.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_load_streamable_http_fields(tmp_path):
    p = _write_cfg(tmp_path, """
        client:
          servers:
            - name: alpha
              transport: streamable_http
              url: "https://alpha.example/mcp"
              api_key_env: RELAY_TOKEN_ALPHA
              headers:
                X-Custom: "v"
              gate_tools: ["run", "restart_*", "firewall_set_rules"]
              only_tools: ["system_info", "disk_*"]
              timeout_s: 60
              max_calls_per_session: 40
    """)
    cfg = load_mcp_config(p)
    assert len(cfg.servers) == 1
    s = cfg.servers[0]
    assert s.transport == "streamable_http"
    assert s.url == "https://alpha.example/mcp"
    assert s.api_key_env == "RELAY_TOKEN_ALPHA"
    assert s.headers == {"X-Custom": "v"}
    assert s.gate_tools == ["run", "restart_*", "firewall_set_rules"]
    assert s.only_tools == ["system_info", "disk_*"]
    assert s.timeout_s == 60.0
    assert s.max_calls_per_session == 40


def test_load_defaults_keep_old_shape(tmp_path):
    p = _write_cfg(tmp_path, """
        client:
          servers:
            - name: git
              transport: stdio
              command: uvx
              args: ["mcp-server-git"]
    """)
    s = load_mcp_config(p).servers[0]
    assert s.headers == {} and s.api_key_env == ""
    assert s.gate_tools == [] and s.only_tools == []


def test_tool_gated_fnmatch():
    s = MCPServerConfig(name="x", gate_tools=["run", "restart_*", "reboot_*"])
    assert s.tool_gated("run")
    assert s.tool_gated("restart_container")
    assert s.tool_gated("reboot_router")
    assert not s.tool_gated("system_info")
    assert not s.tool_gated("firewall_status")


def test_tool_allowed_empty_means_all():
    s = MCPServerConfig(name="x")
    assert s.tool_allowed("anything")
    s2 = MCPServerConfig(name="y", only_tools=["disk_*", "system_info"])
    assert s2.tool_allowed("disk_usage")
    assert s2.tool_allowed("system_info")
    assert not s2.tool_allowed("run")


def test_manager_is_write_gated_resolves_namespaced_names():
    cfg = MCPClientConfig(servers=[
        MCPServerConfig(name="alpha", gate_tools=["run", "restart_*"]),
        MCPServerConfig(name="router1", gate_tools=["reboot_router"]),
    ])
    mgr = MCPClientManager(cfg)
    for s in cfg.servers:
        mgr._servers[s.name] = _ServerRuntime(cfg=s)
    assert mgr.is_write_gated("mcp__alpha__run")
    assert mgr.is_write_gated("mcp__alpha__restart_container")
    assert not mgr.is_write_gated("mcp__alpha__system_info")
    assert mgr.is_write_gated("mcp__router1__reboot_router")
    assert not mgr.is_write_gated("mcp__router1__run")  # not gated on router1
    assert not mgr.is_write_gated("mcp__unknown__run")  # unknown server
    assert not mgr.is_write_gated("read_file")          # not namespaced


def test_bad_transport_marks_down_not_raise():
    cfg = MCPClientConfig(servers=[
        MCPServerConfig(name="weird", transport="carrier_pigeon"),
    ])
    mgr = MCPClientManager(cfg).start()
    try:
        status = mgr.server_status()
        assert len(status) == 1 and status[0]["down"]
        defs, fns = mgr.discover_tools()
        assert defs == [] and fns == {}
    finally:
        mgr.close()


def test_http_requires_url_marks_down_not_raise():
    cfg = MCPClientConfig(servers=[
        MCPServerConfig(name="nourl", transport="streamable_http"),
    ])
    mgr = MCPClientManager(cfg).start()
    try:
        status = mgr.server_status()
        assert status[0]["down"]
        assert "url" in status[0]["down_reason"]
    finally:
        mgr.close()


def test_mcptools_registry_roundtrip():
    assert mcptools.active() is None
    d1 = ToolDef(name="mcp__alpha__system_info", description="d", parameters={"type": "object"})
    d2 = ToolDef(name="mcp__alpha__run", description="d", parameters={"type": "object"})
    surf = mcptools.MCPSurface(always_defs=[d1], gated_defs=[d2],
                               fns={d1.name: lambda a: ("", None),
                                    d2.name: lambda a: ("", None)})
    mcptools.set_surface(surf)
    try:
        got = mcptools.active()
        assert got is surf
        assert [d.name for d in got.always_defs] == [d1.name]
        assert [d.name for d in got.gated_defs] == [d2.name]
    finally:
        mcptools.clear()
    assert mcptools.active() is None
