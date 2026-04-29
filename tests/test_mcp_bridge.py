"""Tests for src/luxe/mcp/bridge.py — MCP type translation."""

from __future__ import annotations

from dataclasses import dataclass

from luxe.mcp.bridge import (
    make_mcp_tool_fn,
    mcp_tool_to_tooldef,
    namespace_tool_name,
    render_mcp_call_result,
    split_namespaced_name,
)


@dataclass
class _FakeMCPTool:
    name: str
    description: str
    inputSchema: dict


@dataclass
class _FakeContent:
    type: str
    text: str = ""
    uri: str = ""


def test_namespace_tool_name():
    assert namespace_tool_name("github", "create_issue") == "mcp__github__create_issue"


def test_split_namespaced_name_valid():
    assert split_namespaced_name("mcp__github__create_issue") == ("github", "create_issue")


def test_split_namespaced_name_native_returns_none():
    assert split_namespaced_name("read_file") is None
    assert split_namespaced_name("mcp__only_one_segment") is None


def test_mcp_tool_to_tooldef_basic():
    mcp_tool = _FakeMCPTool(
        name="create_issue",
        description="Create a GitHub issue",
        inputSchema={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    )
    td = mcp_tool_to_tooldef(mcp_tool, "github")
    assert td.name == "mcp__github__create_issue"
    assert "GitHub issue" in td.description
    assert "via MCP server" in td.description
    assert td.parameters["properties"]["title"]["type"] == "string"


def test_mcp_tool_to_tooldef_synthesizes_object_type():
    """Some MCP tools omit 'type' at the schema root; we add it."""
    mcp_tool = _FakeMCPTool(
        name="ping",
        description="",
        inputSchema={"properties": {}, "required": []},
    )
    td = mcp_tool_to_tooldef(mcp_tool, "srv")
    assert td.parameters["type"] == "object"


def test_render_mcp_call_result_text_only():
    parts = [_FakeContent(type="text", text="hello"),
             _FakeContent(type="text", text="world")]
    assert render_mcp_call_result(parts) == "hello\nworld"


def test_render_mcp_call_result_image_placeholder():
    parts = [_FakeContent(type="text", text="result:"),
             _FakeContent(type="image")]
    out = render_mcp_call_result(parts)
    assert "result:" in out
    assert "[media: image" in out


def test_render_mcp_call_result_resource_placeholder():
    parts = [_FakeContent(type="resource", uri="file:///foo")]
    out = render_mcp_call_result(parts)
    assert "[media: resource file:///foo" in out


def test_make_mcp_tool_fn_threads_args_to_sync_call():
    captured: list[tuple[str, str, dict]] = []

    def sync_call(server, tool, args):
        captured.append((server, tool, args))
        return "ok", None

    fn = make_mcp_tool_fn(sync_call, "github", "create_issue")
    text, err = fn({"title": "bug"})
    assert text == "ok"
    assert err is None
    assert captured == [("github", "create_issue", {"title": "bug"})]


def test_make_mcp_tool_fn_propagates_error():
    def sync_call(server, tool, args):
        return "", "boom"
    fn = make_mcp_tool_fn(sync_call, "github", "x")
    text, err = fn({})
    assert text == ""
    assert err == "boom"
