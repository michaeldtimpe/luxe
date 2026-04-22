"""Web tools: DuckDuckGo search + URL fetch (main-content extraction).

Uses `ddgs` for search and `trafilatura` for extracting readable text from
HTML. Kept intentionally small — the agent calls these as tools, so each
function returns a compact dict that slots into the tool-result channel.
"""

from __future__ import annotations

import json
from typing import Any

from harness.backends import ToolDef

MAX_RESULTS_CAP = 8
MAX_FETCH_CHARS = 12000


def tool_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name="web_search",
            description=(
                "Search the web via DuckDuckGo. Returns a JSON list of "
                "{title, url, snippet}. Use this first when you need facts "
                "that may change over time (news, versions, current events)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": MAX_RESULTS_CAP,
                    },
                },
                "required": ["query"],
            },
        ),
        ToolDef(
            name="fetch_url",
            description=(
                "Fetch a URL and return its readable main content as plain "
                "text (headers/navs/scripts stripped). Use after web_search "
                "to read the most promising results."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Absolute http(s) URL."},
                },
                "required": ["url"],
            },
        ),
    ]


def web_search(args: dict[str, Any]) -> tuple[Any, str | None]:
    from ddgs import DDGS

    query = args.get("query", "").strip()
    if not query:
        return None, "empty query"
    n = max(1, min(int(args.get("max_results") or 5), MAX_RESULTS_CAP))
    try:
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=n))
    except Exception as e:  # noqa: BLE001
        return None, f"search failed: {type(e).__name__}: {e}"

    results = [
        {
            "title": (r.get("title") or "").strip(),
            "url": r.get("href") or r.get("url") or "",
            "snippet": (r.get("body") or "").strip()[:400],
        }
        for r in raw
        if r.get("href") or r.get("url")
    ]
    return json.dumps(results, ensure_ascii=False), None


def fetch_url(args: dict[str, Any]) -> tuple[Any, str | None]:
    import trafilatura

    url = args.get("url", "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return None, "url must be absolute http(s)"

    try:
        html = trafilatura.fetch_url(url)
    except Exception as e:  # noqa: BLE001
        return None, f"fetch failed: {type(e).__name__}: {e}"
    if not html:
        return None, "fetch returned empty response"

    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    ) or ""
    text = text.strip()
    if not text:
        return None, "no readable content extracted"

    truncated = len(text) > MAX_FETCH_CHARS
    if truncated:
        text = text[:MAX_FETCH_CHARS]
    return (
        json.dumps(
            {"url": url, "text": text, "truncated": truncated},
            ensure_ascii=False,
        ),
        None,
    )


# Dispatch table for agent wiring.
TOOL_FNS = {
    "web_search": web_search,
    "fetch_url": fetch_url,
}
