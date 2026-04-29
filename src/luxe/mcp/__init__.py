"""Model Context Protocol — client + server integration for luxe.

The `mcp` package is a runtime requirement for v1.0. We surface MCP_AVAILABLE
for code paths that may want to short-circuit cleanly if the package is
broken or absent (e.g. minimal test environments).
"""

from __future__ import annotations

try:
    import mcp  # noqa: F401
    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover — package is required in pyproject
    MCP_AVAILABLE = False

__all__ = ["MCP_AVAILABLE"]
