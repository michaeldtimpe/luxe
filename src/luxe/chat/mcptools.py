"""Chat-only registry for MCP tools injected into interactive turns.

`luxe chat --mcp <name>` starts an MCPClientManager (cli) and publishes the
discovered tool surface here; `prepare_turn` (repl.py — shared by both
front-ends) reads it and extends the turn's extra-tool seam. Module-level like
`search.set_index` so the surface doesn't have to thread through both
front-end signatures.

Write gating: `always_defs` ride every turn; `gated_defs` (the server's
`gate_tools` matches — mutating remote operations) are appended only when the
session is in write mode. The benchmark/maintain path never touches this
module, so its MCP behavior (configs/mcp.yaml `enabled_for`) is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from luxe.tools.base import ToolDef, ToolFn


@dataclass(frozen=True)
class MCPSurface:
    always_defs: list[ToolDef] = field(default_factory=list)
    gated_defs: list[ToolDef] = field(default_factory=list)
    fns: dict[str, ToolFn] = field(default_factory=dict)
    # MCPClientManager.server_status — for /tools, /status, /doctor.
    status_fn: Callable[[], list[dict[str, Any]]] | None = None


_active: MCPSurface | None = None


def set_surface(surface: MCPSurface) -> None:
    global _active
    _active = surface


def clear() -> None:
    global _active
    _active = None


def active() -> MCPSurface | None:
    return _active
