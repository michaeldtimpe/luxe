"""luxe PDF tools — an opt-in stdio MCP server.

Not part of the benchmark tool surface. `luxe.mcp_pdf.ops` holds the plain
functions (importable and testable without an MCP session);
`luxe.mcp_pdf.server` wraps them as MCP tools behind the `luxe-pdf-mcp`
console script.
"""

from luxe.mcp_pdf.ops import PdfToolError

__all__ = ["PdfToolError"]
