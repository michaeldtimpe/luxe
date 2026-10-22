"""Role-config transforms shared across front-ends.

`make_read_only_role` is used by gitkit (every read-only analysis), the chat
turn (read-only until `/write`), and the `luxe serve` read-only pipeline
(cli/maint.py). It lived in `mcp/server.py` until 2026-10, which made
those callers import the MCP server module to strip three tool names;
`luxe.mcp.server` still re-exports both names. Root tier: imports nothing
from luxe.
"""

from __future__ import annotations

from typing import Any

#: Tools `make_read_only_role` strips. `git_diff` is read-only and stays.
MUTATION_TOOL_NAMES = {
    "write_file", "edit_file", "bash",
}


def make_read_only_role(role_cfg) -> Any:
    """Return a copy of role_cfg with mutation tools stripped from `tools`."""
    new_tools = [t for t in (role_cfg.tools or []) if t not in MUTATION_TOOL_NAMES]
    return role_cfg.model_copy(update={"tools": new_tools})
