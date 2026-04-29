"""Translation layer between MCP types and luxe's ToolDef/ToolFn primitives.

MCP tools are namespaced as `mcp__{server}__{tool}` so they can't collide with
native tools. The bridge produces:
- A ToolDef whose `parameters` is the MCP tool's inputSchema (already JSON
  Schema — no transformation needed).
- A ToolFn that synchronously calls back into the MCPClientManager's async
  event loop via the sync_call() helper.

Output handling: MCP returns a list of content parts (TextContent / ImageContent
/ etc.). We concatenate text parts; non-text parts are reported via a [media]
placeholder so downstream tools see something coherent.
"""

from __future__ import annotations

from typing import Any, Callable

from luxe.tools.base import ToolDef, ToolFn


def namespace_tool_name(server_name: str, tool_name: str) -> str:
    """`server` + `tool` → `mcp__server__tool`. Inverse of split_namespaced_name."""
    return f"mcp__{server_name}__{tool_name}"


def split_namespaced_name(name: str) -> tuple[str, str] | None:
    if not name.startswith("mcp__"):
        return None
    parts = name.split("__", 2)
    if len(parts) != 3:
        return None
    return parts[1], parts[2]


def mcp_tool_to_tooldef(mcp_tool: Any, server_name: str) -> ToolDef:
    """Convert an mcp.types.Tool to a luxe ToolDef. JSON Schema passes through."""
    schema = getattr(mcp_tool, "inputSchema", None) or {}
    if not isinstance(schema, dict):
        try:
            schema = schema.model_dump()
        except Exception:
            schema = {}
    if "type" not in schema:
        schema = {"type": "object", "properties": schema.get("properties", {}),
                  "required": schema.get("required", [])}
    description = (
        getattr(mcp_tool, "description", "") or ""
    ) + f"\n\n[via MCP server `{server_name}`]"
    return ToolDef(
        name=namespace_tool_name(server_name, mcp_tool.name),
        description=description.strip(),
        parameters=schema,
    )


def render_mcp_call_result(content_parts: list[Any]) -> str:
    """Concatenate the text portions of an MCP CallToolResult.content list."""
    out: list[str] = []
    for part in content_parts:
        ptype = getattr(part, "type", None)
        if ptype == "text":
            out.append(getattr(part, "text", "") or "")
        elif ptype == "image":
            out.append("[media: image attachment omitted]")
        elif ptype == "resource":
            uri = getattr(part, "uri", "") or "[unknown]"
            out.append(f"[media: resource {uri} omitted]")
        else:
            text = getattr(part, "text", None)
            if text is not None:
                out.append(text)
    return "\n".join(out).strip()


def make_mcp_tool_fn(
    sync_call: Callable[[str, str, dict], tuple[str, str | None]],
    server_name: str,
    tool_name: str,
) -> ToolFn:
    """Wrap the MCPClientManager's sync_call into a luxe ToolFn.

    sync_call returns (result_text, error_text). On error the ToolFn yields
    (empty_string, error_text) so dispatch_tool surfaces the failure to the
    model with the standard schema-error path.
    """
    def _fn(args: dict[str, Any]) -> tuple[str, str | None]:
        return sync_call(server_name, tool_name, args)
    _fn.__name__ = f"mcp_call_{server_name}_{tool_name}"
    return _fn
