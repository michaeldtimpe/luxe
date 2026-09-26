"""Headless-browser rendering via Playwright — optional, absent by default.

`luxe[web]` installs the Python package; the Chromium binary is a further
`playwright install chromium` (~150-400 MB). Neither is a base dependency:
the fallback kit must install and run on every fleet host without a browser
download, and most fetches (docs, RFCs, GitHub, JSON APIs) never need one.

`availability()` is the single place that answers "can we render?", so every
surface — the tool, `/web`, `/doctor` — reports the same thing and names the
exact missing step rather than raising ImportError at the model.

The same egress guard as `fetch.py` applies before the browser is launched
AND to every request the browser makes afterwards (`EgressGuard`). A real
browser is a much better SSRF weapon than httpx: it follows redirects, runs
JS that can issue its own requests, and speaks whatever the page asks.
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


def _system_chrome() -> str | None:
    """An already-installed Chrome/Chromium, if there is one.

    Carried over from the 2026-08-02 `browser.py` stack, whose real advantage
    was needing NO browser download — it drove the Chrome the user already
    had. Preserving that here makes `playwright install chromium` an
    optimisation rather than a hard requirement on every fleet host.
    """
    import os
    import shutil

    for candidate in ("google-chrome", "google-chrome-stable", "chromium",
                      "chromium-browser"):
        found = shutil.which(candidate)
        if found:
            return found
    for path in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Chromium.app/Contents/MacOS/Chromium"):
        if os.path.exists(path):
            return path
    return None


@functools.lru_cache(maxsize=1)
def availability() -> Availability:
    """Can we render right now? Distinguishes the failure modes.

    Deliberately does NOT start the Playwright driver. `sync_playwright()`
    spawns a Node subprocess, and tearing it back down emits asyncio
    "Task was destroyed but it is pending" / TargetClosedError chatter on
    stderr — which would land in the middle of a chat session every time the
    status line or /doctor asked a simple capability question. Import plus a
    filesystem check answer it for free; a genuine launch failure still
    surfaces as a clean WebError from render_url.

    Cached: the answer cannot change within a process without an install.
    """
    try:
        import playwright  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return Availability(
            ok=False,
            reason="playwright is not installed",
            fix="uv sync --extra web  (then optionally: "
                ".venv/bin/playwright install chromium)",
        )
    root = _browsers_root()
    try:
        installed = any(p.is_dir() and p.name.startswith("chromium")
                        for p in root.iterdir())
    except OSError:
        installed = False
    if installed:
        return Availability(ok=True)
    if _system_chrome():
        return Availability(ok=True)
    return Availability(
        ok=False,
        reason=f"no Chromium in {root} and no system Chrome found",
        fix=".venv/bin/playwright install chromium  (or install Google Chrome)",
    )


_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/120.0 Safari/537.36 luxe/1.0")


class EgressGuard:
    """The egress guard applied INSIDE the browser, to every request.

    Checking only the top-level URL left the browser free to fetch anything a
    page asked for: subresources, XHR/fetch, WebSockets, service-worker
    traffic — all of which could reach localhost, the tailnet, or cloud
    metadata. Installed on the context, so popups and frames inherit it:

    - every http(s) request is routed through `resolve_public`; a refusal
      aborts it (`blockedbyclient`), any non-http(s) network scheme is
      aborted outright;
    - every WebSocket is routed the same way and left unconnected on refusal;
    - service workers are blocked at context creation (their fetches bypass
      routing entirely);
    - Playwright routes only the FIRST url of a redirect chain, so allowed
      requests are fetched with `max_redirects=0` and the 3xx is handed back
      to the browser: each hop becomes a new, routed, checked request;
    - any response that still arrives from non-public space is recorded in
      `escaped`, and the caller treats an entry there as a hard stop.

    Cost: allowed requests are fetched by Playwright and fulfilled into the
    page (buffered, not streamed) — fine for reading pages, which is the job.

    Residual, stated honestly: Chromium resolves names itself, so a
    rebinding name can still differ between this check and the browser's
    connect. The httpx path (`fetch.py`) pins the connect; the browser
    cannot, which is one more reason `render=true` is opt-in.
    """

    def __init__(self):
        self._verdicts: dict[tuple[str, str], str] = {}   # origin → "" | why
        self.blocked: list[str] = []
        self.escaped: list[str] = []

    def install(self, context) -> "EgressGuard":
        context.route("**/*", self.on_route)
        context.route_web_socket("**/*", self.on_websocket)
        context.on("response", self.on_response)
        return self

    def verdict(self, url: str) -> str:
        """Empty string when `url` may be fetched, else the refusal reason."""
        from urllib.parse import urlsplit

        try:
            parts = urlsplit(url)
            key = (parts.scheme.lower(), parts.netloc.lower())
        except ValueError:
            return f"unparseable URL {url!r}"
        if key[0] in ("ws", "wss"):
            url = ("https" if key[0] == "wss" else "http") + url[len(key[0]):]
            key = ("https" if key[0] == "wss" else "http", key[1])
        if key[0] not in ("http", "https"):
            return f"refused scheme `{key[0]}`"
        if key not in self._verdicts:
            try:
                _assert_public(url)
                self._verdicts[key] = ""
            except WebError as e:
                self._verdicts[key] = str(e)
        return self._verdicts[key]

    def on_route(self, route) -> None:
        url = route.request.url
        why = self.verdict(url)
        if why:
            self.blocked.append(url)
            route.abort("blockedbyclient")
            return
        # route.continue_() would let Chromium follow a redirect chain on its
        # own, and Playwright never routes the later hops. Fetching with
        # max_redirects=0 hands the 3xx back to the browser, whose follow-up
        # request is a NEW request — routed, and so checked, like any other.
        try:
            response = route.fetch(max_redirects=0)
        except Exception:  # noqa: BLE001 — network failure: fail the request
            route.abort("failed")
            return
        route.fulfill(response=response)

    def on_websocket(self, ws) -> None:
        # A routed WebSocket reaches its server ONLY if the handler calls
        # connect_to_server(); leaving it unconnected is the refusal. (Calling
        # ws.close() from inside a sync-API handler deadlocks the driver —
        # found by the live drill, so don't "tidy" this into a close.)
        if self.verdict(ws.url):
            self.blocked.append(ws.url)
            return
        ws.connect_to_server()

    def on_response(self, response) -> None:
        # Belt and braces: a response from a non-public URL means something
        # reached the network without passing on_route. Nothing should.
        if self.verdict(response.url):
            self.escaped.append(response.url)

    def check(self) -> None:
        """Raise if any response came back from non-public space."""
        if self.escaped:
            raise WebError(
                f"refused: the page reached the non-public address "
                f"{self.escaped[0]} — luxe will not follow a page into "
                "private, loopback or tailnet space")


def launch_guarded(p):
    """Launch headless Chromium; return (browser, context, guard).

    The ONE launch path for `render_url` and the `web_page` session.
    Prefers Playwright's own Chromium and falls back to the system Chrome via
    executable_path, so a host without the download still renders.
    """
    launch_kwargs: dict = {"headless": True}
    root = _browsers_root()
    try:
        have_download = any(q.is_dir() and q.name.startswith("chromium")
                            for q in root.iterdir())
    except OSError:
        have_download = False
    if not have_download:
        system = _system_chrome()
        if system:
            launch_kwargs["executable_path"] = system
    browser = p.chromium.launch(**launch_kwargs)
    try:
        context = browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 2000},
            service_workers="block",
        )
        guard = EgressGuard().install(context)
    except BaseException:
        browser.close()
        raise
    return browser, context, guard


def render_url(url: str, *, timeout_s: float = DEFAULT_RENDER_TIMEOUT_S,
               wait_for: str = "") -> tuple[str, str, str]:
    """Load `url` in headless Chromium and return (final_url, title, html).

    `wait_for` is an optional CSS selector to await before reading the DOM —
    the reliable way to handle a page whose content arrives after load.
    """
    avail = availability()
    if not avail.ok:
        raise WebError(f"cannot render: {avail.reason}. Fix: {avail.fix}")

    # The browser gets the re-serialized, checked URL — never the model's
    # original string, which a WHATWG parser might read differently.
    target = _assert_public(url)

    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    timeout_ms = int(timeout_s * 1000)
    with sync_playwright() as p:
        browser, context, guard = launch_guarded(p)
        try:
            page = context.new_page()
            page.goto(target.url, timeout=timeout_ms,
                      wait_until="domcontentloaded")
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
            guard.check()
            final_url = page.url
            _assert_public(final_url)  # JS may have navigated us somewhere else
            title = page.title() or ""
            return final_url, title, page.content()
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


__all__ = ["Availability", "EgressGuard", "availability", "launch_guarded",
           "render_url", "render_to_result", "DEFAULT_RENDER_TIMEOUT_S",
           "DEFAULT_TIMEOUT_S"]
