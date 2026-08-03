"""Web search via a pluggable provider, keyed through `luxe.secrets`.

Key resolution is the same rule the oMLX backends and the mage-hands relays
follow: env → ~/.luxe/secrets.env → login Keychain, by env-var NAME. A key
value never appears in YAML or in this repo.

Provider selection is by which key is present, in `_PROVIDERS` order, so a
host with no key simply has no `web_search` tool — `configured()` says so and
the chat layer withholds the tool rather than shipping one that always
errors. That is the fallback-kit posture: degrade quietly and say why.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_COUNT = 5
MAX_COUNT = 20
SEARCH_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class Provider:
    name: str
    env_name: str
    signup: str


# Order = preference when several keys are present.
_PROVIDERS: tuple[Provider, ...] = (
    Provider("brave", "BRAVE_API_KEY", "https://brave.com/search/api/"),
    Provider("tavily", "TAVILY_API_KEY", "https://tavily.com/"),
)


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""


def _key_for(p: Provider) -> str:
    from luxe.secrets import resolve_api_key
    try:
        return resolve_api_key(p.env_name) or ""
    except Exception:
        return ""


def active_provider() -> tuple[Provider, str] | None:
    """(provider, key) for the first provider with a resolvable key."""
    for p in _PROVIDERS:
        key = _key_for(p)
        if key:
            return p, key
    return None


def configured() -> bool:
    return active_provider() is not None


def missing_key_message() -> str:
    names = ", ".join(f"{p.env_name} ({p.signup})" for p in _PROVIDERS)
    return ("no web-search API key found. luxe looks for these, in order, via "
            f"env → ~/.luxe/secrets.env → Keychain: {names}. Add one of them "
            "to ~/.luxe/secrets.env (the NAME goes in config, never the value).")


def _search_brave(query: str, key: str, count: int) -> list[SearchHit]:
    import httpx

    r = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": count},
        headers={"Accept": "application/json", "X-Subscription-Token": key},
        timeout=SEARCH_TIMEOUT_S,
    )
    r.raise_for_status()
    data = r.json()
    hits = []
    for item in (data.get("web", {}) or {}).get("results", [])[:count]:
        hits.append(SearchHit(
            title=str(item.get("title", "")),
            url=str(item.get("url", "")),
            snippet=str(item.get("description", "")),
        ))
    return hits


def _search_tavily(query: str, key: str, count: int) -> list[SearchHit]:
    import httpx

    r = httpx.post(
        "https://api.tavily.com/search",
        json={"api_key": key, "query": query, "max_results": count},
        timeout=SEARCH_TIMEOUT_S,
    )
    r.raise_for_status()
    data = r.json()
    hits = []
    for item in data.get("results", [])[:count]:
        hits.append(SearchHit(
            title=str(item.get("title", "")),
            url=str(item.get("url", "")),
            snippet=str(item.get("content", "")),
        ))
    return hits


_IMPLS = {"brave": _search_brave, "tavily": _search_tavily}


def search(query: str, *, count: int = DEFAULT_COUNT) -> tuple[str, list[SearchHit]]:
    """(provider_name, hits). Raises WebError when unusable."""
    from luxe.web.fetch import WebError

    query = (query or "").strip()
    if not query:
        raise WebError("empty search query")
    active = active_provider()
    if active is None:
        raise WebError(missing_key_message())
    provider, key = active
    count = max(1, min(int(count or DEFAULT_COUNT), MAX_COUNT))
    try:
        hits = _IMPLS[provider.name](query, key, count)
    except Exception as e:
        import httpx
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
            raise WebError(
                f"{provider.name} rejected the API key in {provider.env_name} "
                f"(HTTP {e.response.status_code}) — check it is current") from e
        raise WebError(f"{provider.name} search failed: {type(e).__name__}: {e}") from e
    return provider.name, hits


def render_hits(provider: str, query: str, hits: list[SearchHit]) -> str:
    if not hits:
        return f"no results for {query!r} (via {provider})"
    lines = [f"{len(hits)} result(s) for {query!r} — via {provider}", ""]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. {h.title}")
        lines.append(f"   {h.url}")
        if h.snippet:
            lines.append(f"   {h.snippet.strip()}")
        lines.append("")
    lines.append("Use web_fetch(url=…) to read any of these in full.")
    return "\n".join(lines).strip()
