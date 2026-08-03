"""ToolDef/ToolFn factories for the chat extra-tool seam.

Same shape as `netdiag.make_net_probe_tool` and `make_update_ledger_tool`:
build (def, fn) pairs, hand them to `run_single`'s extra-tool seam. Nothing
here is ever added to `TOOL_FNS` — the benchmark/maintain path must not gain
a tool whose output depends on the live internet (tools.sdd, web.sdd).

Errors return as `(str, str | None)` like every other luxe tool and never
raise into the agent loop.
"""

from __future__ import annotations

from luxe.tools.base import ToolDef
from luxe.web.fetch import WebError

_FETCH_DESC = (
    "Fetch a web page or file over HTTP(S) and return its readable content as "
    "markdown. Use this to read documentation, articles, RFCs, source files, "
    "issue threads, or JSON APIs. HTML is stripped to prose, headings, code "
    "blocks and links; JSON and plain text pass through unchanged. If the "
    "result comes back nearly empty the page is JavaScript-rendered — call "
    "again with render=true to load it in a real headless browser. Only "
    "public http/https addresses are reachable; private, loopback and tailnet "
    "hosts are refused."
)

_FETCH_PARAMS = {
    "type": "object",
    "properties": {
        "url": {"type": "string",
                "description": "Absolute URL. https:// is assumed if omitted."},
        "render": {
            "type": "boolean",
            "description": "Load in a headless browser so JavaScript runs. "
                           "Slower (seconds); use when a plain fetch returned "
                           "little or no text. Default false.",
        },
        "wait_for": {
            "type": "string",
            "description": "With render=true, a CSS selector to wait for "
                           "before reading the page (for late-loading content).",
        },
        "max_chars": {
            "type": "integer",
            "description": "Cap on returned characters (default 20000).",
        },
    },
    "required": ["url"],
}

_SEARCH_DESC = (
    "Search the web and return ranked results with titles, URLs and snippets. "
    "Use it when you do not already know the URL; then call web_fetch on the "
    "result you want to read in full. Snippets are not sources — quote or "
    "rely only on content you actually fetched. For a quick factual question "
    "where one synthesized answer is enough, web_answer (when available) is "
    "faster than searching and reading."
)

_SEARCH_PARAMS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query."},
        "count": {"type": "integer",
                  "description": "Number of results, 1-20 (default 5)."},
    },
    "required": ["query"],
}

_ANSWER_DESC = (
    "Ask a question and get one AI-generated answer grounded in a live web "
    "search, with the synthesis done server-side (Brave Answers). This is a "
    "DIFFERENT feature from web_search: web_search returns ranked links for "
    "you to read via web_fetch; web_answer returns a finished answer. Prefer "
    "web_answer for quick factual questions; prefer web_search + web_fetch "
    "when you need to inspect sources, read full pages, or verify claims."
)

_ANSWER_PARAMS = {
    "type": "object",
    "properties": {
        "query": {"type": "string",
                  "description": "The question to answer."},
        "model": {"type": "string",
                  "enum": ["brave", "brave-pro"],
                  "description": "Answer model; omit for the provider "
                                 "default. brave-pro is deeper and slower."},
    },
    "required": ["query"],
}


def make_web_fetch_tool():
    """(ToolDef, ToolFn) for `web_fetch`. Read-only; /web-gated by the caller."""
    from luxe.web.extract import to_markdown
    from luxe.web.fetch import fetch_url

    def _fn(args: dict) -> tuple[str, str | None]:
        args = args or {}
        url = str(args.get("url") or "").strip()
        if not url:
            return "", "web_fetch: `url` is required"
        max_chars = int(args.get("max_chars") or 20_000)
        render = bool(args.get("render"))
        wait_for = str(args.get("wait_for") or "").strip()
        try:
            if render:
                from luxe.web.browser import render_to_result
                result = render_to_result(url, wait_for=wait_for)
            else:
                result = fetch_url(url)
            return to_markdown(result, max_chars=max_chars), None
        except WebError as e:
            return "", str(e)
        except Exception as e:
            return "", f"web_fetch failed: {type(e).__name__}: {e}"

    return ToolDef(name="web_fetch", description=_FETCH_DESC,
                   parameters=_FETCH_PARAMS), _fn


def make_web_search_tool():
    """(ToolDef, ToolFn) for `web_search`. Caller withholds it with no key."""
    from luxe.web.search import render_hits, search

    def _fn(args: dict) -> tuple[str, str | None]:
        args = args or {}
        query = str(args.get("query") or "").strip()
        if not query:
            return "", "web_search: `query` is required"
        try:
            provider, hits = search(query, count=int(args.get("count") or 5))
            return render_hits(provider, query, hits), None
        except WebError as e:
            return "", str(e)
        except Exception as e:
            return "", f"web_search failed: {type(e).__name__}: {e}"

    return ToolDef(name="web_search", description=_SEARCH_DESC,
                   parameters=_SEARCH_PARAMS), _fn


def make_web_answer_tool():
    """(ToolDef, ToolFn) for `web_answer`. Caller withholds it with no key."""
    from luxe.web.answers import answer

    def _fn(args: dict) -> tuple[str, str | None]:
        args = args or {}
        query = str(args.get("query") or "").strip()
        if not query:
            return "", "web_answer: `query` is required"
        try:
            return answer(query, model=str(args.get("model") or "")), None
        except WebError as e:
            return "", str(e)
        except Exception as e:
            return "", f"web_answer failed: {type(e).__name__}: {e}"

    return ToolDef(name="web_answer", description=_ANSWER_DESC,
                   parameters=_ANSWER_PARAMS), _fn


def web_tools(*, include_search: bool = True) -> tuple[list, dict]:
    """All enabled web tools as (defs, fns) for the extra-tool seam.

    `web_search` and `web_answer` are each included only when their OWN key
    resolves (they are separate subscriptions) — a tool that can only ever
    return "no API key" is worse than no tool at all.
    """
    from luxe.web.answers import configured as answers_configured
    from luxe.web.search import configured

    defs, fns = [], {}
    d, f = make_web_fetch_tool()
    defs.append(d)
    fns[d.name] = f
    if include_search and configured():
        d, f = make_web_search_tool()
        defs.append(d)
        fns[d.name] = f
    if include_search and answers_configured():
        d, f = make_web_answer_tool()
        defs.append(d)
        fns[d.name] = f
    return defs, fns
