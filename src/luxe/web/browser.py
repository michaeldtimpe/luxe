"""Headless-browser rendering via Playwright — optional, absent by default.

`luxe[web]` installs the Python package; the Chromium binary is a further
`playwright install chromium` (~150-400 MB). Neither is a base dependency:
the fallback kit must install and run on every fleet host without a browser
download, and most fetches (docs, RFCs, GitHub, JSON APIs) never need one.

`availability()` is the single place that answers "can we render?", so every
surface — the tool, `/web`, `/doctor` — reports the same thing and names the
exact missing step rather than raising ImportError at the model.

The same egress guard as `fetch.py` applies before the browser is launched.
A real browser is a much better SSRF weapon than httpx: it follows redirects,
runs JS that can issue its own requests, and speaks whatever the page asks.
So the guard runs first, and JS is loaded with a hard navigation timeout.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

from luxe.web.fetch import DEFAULT_TIMEOUT_S, WebError, _assert_public

# A render is slower than a fetch by construction (browser launch + JS +
# network idle). This ceiling keeps a hung page from eating a chat turn.
DEFAULT_RENDER_TIMEOUT_S = 45.0


@dataclass(frozen=True)
class Availability:
    ok: bool
    reason: str = ""      # why not, when ok is False
    fix: str = ""         # the exact command to make it work


def _browsers_root():
    """Where Playwright keeps downloaded browsers, per its documented rules."""
    import os
    import sys
    from pathlib import Path

    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if override and override != "0":
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


@functools.lru_cache(maxsize=1)
def availability() -> Availability:
    """Can we render right now? Distinguishes the two failure modes.

    Deliberately does NOT start the Playwright driver. `sync_playwright()`
    spawns a Node subprocess, and tearing it back down emits asyncio
    "Task was destroyed but it is pending" / TargetClosedError chatter on
    stderr — which would land in the middle of a chat session every time the
    status line or /doctor asked a simple capability question. Import + an
    on-disk browser directory answer it for free; a genuine launch failure
    still surfaces as a clean WebError from render_url.

    Cached: the answer cannot change within a process without an install.
    """
    try:
        import playwright  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return Availability(
            ok=False,
            reason="playwright is not installed",
            fix="uv sync --extra web && .venv/bin/playwright install chromium",
        )
    root = _browsers_root()
    try:
        installed = any(p.is_dir() and p.name.startswith("chromium")
                        for p in root.iterdir())
    except OSError:
        installed = False
    if not installed:
        return Availability(
            ok=False,
            reason=f"no Chromium browser found in {root}",
            fix=".venv/bin/playwright install chromium",
        )
    return Availability(ok=True)


def render_url(url: str, *, timeout_s: float = DEFAULT_RENDER_TIMEOUT_S,
               wait_for: str = "", full_text: bool = False) -> tuple[str, str, str]:
    """Load `url` in headless Chromium and return (final_url, title, html).

    `wait_for` is an optional CSS selector to await before reading the DOM —
    the reliable way to handle a page whose content arrives after load.
    """
    avail = availability()
    if not avail.ok:
        raise WebError(f"cannot render: {avail.reason}. Fix: {avail.fix}")

    from luxe.web.fetch import _normalize_url
    url = _normalize_url(url)
    _assert_public(url)

    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    timeout_ms = int(timeout_s * 1000)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0 Safari/537.36 luxe/1.0"),
                viewport={"width": 1280, "height": 2000},
            )
            page = context.new_page()
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10_000))
            except Exception:
                pass  # networkidle is best-effort; many pages never go idle
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=min(timeout_ms, 15_000))
                except Exception as e:
                    raise WebError(
                        f"selector {wait_for!r} never appeared: {e}") from e
            final_url = page.url
            _assert_public(final_url)  # JS may have navigated us somewhere else
            title = page.title() or ""
            html = page.content() if not full_text else page.inner_text("body")
            return final_url, title, html
        finally:
            browser.close()


def render_to_result(url: str, *, timeout_s: float = DEFAULT_RENDER_TIMEOUT_S,
                     wait_for: str = ""):
    """Render and adapt to a `FetchResult`, so extraction is path-independent."""
    from luxe.web.fetch import FetchResult

    import time
    started = time.monotonic()
    final_url, _title, html = render_url(url, timeout_s=timeout_s,
                                         wait_for=wait_for)
    return FetchResult(
        url=final_url,
        status=200,
        content_type="text/html; charset=utf-8",
        text=html,
        elapsed_s=time.monotonic() - started,
    )


__all__ = ["Availability", "availability", "render_url", "render_to_result",
           "DEFAULT_RENDER_TIMEOUT_S", "DEFAULT_TIMEOUT_S"]
