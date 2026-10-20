"""Stdio MCP server: luxe's tool layer for an outside harness (harness_ab MCP arm).

The harness bake-off's MCP arm attaches this to Qwen Code / Claude Code so
the outside loop gets the tools luxe's own loop has and theirs lack — BM25
search, the symbol index, the analyzers (ruff / mypy / bandit / pip-audit)
and, on `manage` tasks only, `cve_lookup` — with luxe's own definitions and
descriptions. Read/write/shell/git are left to the harness: it already has
them, and duplicates only dilute the surface.

The surface is built by the same `_build_full_tool_surface` call luxe's loop
makes, filtered to the role allowlist in configs/single_64gb.yaml ∩ the
layer-2 set below, so a tool appears here exactly when luxe would offer it
on the same repo and task type. Every call is appended to `--log` (JSONL) so
usage can be counted independently of the harness's event stream.

Usage (the harness launches it):
    .venv/bin/python scripts/harness_ab_luxe_mcp.py --repo <clone> \
        --task-type <implement|document|manage> --log <fixture dir>/luxe_mcp.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Tools the outside harnesses lack. luxe's read_file/grep/write/edit/bash/git_*
# are deliberately excluded (the harness's own versions are used instead).
LAYER2 = ("bm25_search", "find_symbol", "lint", "typecheck", "security_scan",
          "deps_audit", "cve_lookup")


def build_surface(repo: str, task_type: str):
    """(defs, fns) exactly as luxe's loop would offer them, ∩ LAYER2."""
    from luxe import search as search_mod
    from luxe import symbols as symbols_mod
    from luxe.agents.single import _build_full_tool_surface
    from luxe.config import load_config
    from luxe.repo_index import _detect_languages_for_repo
    from luxe.tools.fs import set_repo_root

    set_repo_root(repo)
    search_mod.set_index(search_mod.build_bm25_index(repo))
    symbols_mod.set_index(symbols_mod.build_symbol_index(repo))
    role_tools = load_config().role("monolith").tools or []
    allow = [t for t in role_tools if t in LAYER2]
    return _build_full_tool_surface(_detect_languages_for_repo(repo), allow,
                                    task_type=task_type)


def _log(path: Path | None, record: dict) -> None:
    if path is None:
        return
    with path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


def _payload_status(result: str | None) -> str | None:
    """The analyzers report a failed run (e.g. pip-audit offline) as a normal
    result whose JSON says `"status": "error"` — record it so the usage
    digest doesn't count those calls as clean."""
    try:
        doc = json.loads(result or "")
    except ValueError:
        return None
    return doc.get("status") if isinstance(doc, dict) else None


def make_server(defs, fns, log: Path | None):
    from mcp import types
    from mcp.server.lowlevel import Server

    from luxe.tools.base import dispatch_tool

    server = Server("luxe")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(name=d.name, description=d.description,
                           inputSchema=d.parameters) for d in defs]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        t0 = time.time()
        tc = await asyncio.to_thread(dispatch_tool, name, arguments or {}, fns)
        _log(log, {"ts": t0, "tool": name, "args": arguments,
                   "wall_s": round(time.time() - t0, 3), "error": tc.error,
                   "status": _payload_status(tc.result),
                   "bytes_out": len(tc.result or "")})
        if tc.error:
            raise RuntimeError(tc.error)  # surfaces as isError=True
        return [types.TextContent(type="text", text=tc.result)]

    return server


async def _serve(server) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--task-type", required=True)
    ap.add_argument("--log", type=Path)
    ap.add_argument("--list", action="store_true",
                    help="print the tool names and exit (no server)")
    args = ap.parse_args(argv)
    defs, fns = build_surface(args.repo, args.task_type)
    if args.list:
        print("\n".join(d.name for d in defs))
        return 0
    _log(args.log, {"ts": time.time(), "event": "start", "repo": args.repo,
                    "task_type": args.task_type, "tools": [d.name for d in defs]})
    asyncio.run(_serve(make_server(defs, fns, args.log)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
