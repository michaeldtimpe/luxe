"""Chat-only registry for MCP tools injected into interactive turns.

`luxe chat --mcp <name>` starts an MCPClientManager (cli) and publishes the
discovered tool surface here; `prepare_turn` (repl.py — shared by both
front-ends) reads it and extends the turn's extra-tool seam. Module-level like
`search.set_index` so the surface doesn't have to thread through both
front-end signatures.

Write gating: `always_defs` ride every turn; `gated_defs` (the server's
`gate_tools` matches — mutating remote operations) are appended only when the
session is in write mode. The FNS follow the same gate (`fns_for`): the loop
dispatches on registered fns, not offered defs, so registering a gated fn
while read-only let a model that named the tool anyway (from history, a guess,
or injected page text) run a mutating remote operation with `/write` off. The benchmark/maintain path never touches this
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

    def fns_for(self, write_on: bool) -> dict[str, ToolFn]:
        """The fns to register this turn. Read-only: every gated name maps to
        a stub that explains the gate (same wording contract as
        `tools.fs.make_write_gated_fns`) and never reaches the server."""
        if write_on:
            return dict(self.fns)
        gated = {d.name for d in self.gated_defs}
        out = {k: v for k, v in self.fns.items() if k not in gated}
        out.update({name: _gated_stub(name) for name in gated})
        return out


def _gated_stub(name: str) -> ToolFn:
    def _gated(args: dict[str, Any]) -> tuple[str, str | None]:
        return "", (
            f"{name} is DISABLED: this session is read-only, and this MCP tool "
            f"performs a mutating remote operation. The tool exists — it is "
            f"gated, not missing. Nothing was sent to the server. Do not retry "
            f"this call; tell the user to run /write to enable write mode, "
            f"then continue."
        )
    return _gated


_active: MCPSurface | None = None


def set_surface(surface: MCPSurface) -> None:
    global _active
    _active = surface


def clear() -> None:
    global _active
    _active = None


def active() -> MCPSurface | None:
    return _active
