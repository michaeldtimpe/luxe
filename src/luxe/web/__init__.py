"""Web access for `luxe chat` — bounded fetch, extraction, render, search.

Everything here is CHAT-ONLY and gated behind `/web` (default OFF). The
benchmark/maintain path must never see these tools: a deterministic eval
cannot depend on the live internet. See `web/web.sdd`.
"""

from luxe.web.fetch import FetchResult, WebError, fetch_url
from luxe.web.extract import extract_text

__all__ = ["FetchResult", "WebError", "fetch_url", "extract_text"]
