"""Browser tools: CDP-bridged headless-Chrome navigation + page read.

Restored 2026-08-02 from commit ff6eab7 (`luxe/luxe_cli/tools/browser.py`)
and adapted to the current layout: it lives TOP-LEVEL like netdiag/planeproxy
and registers on the chat extra-tool seam (repl.prepare_turn) — `tools/` is
the benchmark-path registry with sdd obligations, and these tools must never
ride the benchmark path.

Two tools, fixed schema, allowlist-gated:

  browse_navigate(url)  → load a JS-rendered page in a real browser.
  browse_read()         → return the rendered DOM as plain text (trafilatura).

This deliberately does NOT expose click / fill / screenshot — the read-only
surface unblocks JS-rendered content without widening the mutation surface.

Chrome is launched lazily on first call (single instance, headless,
remote-debugging-port) and torn down at process exit. The CDP client is
`pychrome` (sync, small, no node toolchain).

Allowlist: module-level `DEFAULT_BROWSER_ALLOWLIST` tuple,
`set_browser_allowlist()` setter, fnmatch-style host patterns. Empty
allowlist = deny-all (safer default than open). Override via the
`LUXE_BROWSER_ALLOWLIST` env var (comma-separated patterns) at module load.

Dependencies (`pychrome`, `trafilatura`) are the OPTIONAL `[browser]` extra
and are imported LAZILY inside the tool bodies: importing this module must
always succeed, and a missing dep returns a clear actionable tool error
("uv sync --extra browser"), never an ImportError.
"""

from __future__ import annotations

import atexit
import fnmatch
import json
import os
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

from luxe.tools.base import ToolDef, ToolFn

MAX_BROWSE_CHARS = 12000
_CHROME_REMOTE_PORT = 9222
_NAV_TIMEOUT_S = 30.0

_MISSING_DEP_HINT = ("browser tools need the [browser] extra: "
                     "uv sync --extra browser")

# Conservative starter set — well-known docs / dev sites unlikely to
# host hostile content. Override per-environment via env var or
# set_browser_allowlist() in the REPL.
DEFAULT_BROWSER_ALLOWLIST = (
    "github.com",
    "*.github.com",
    "developer.mozilla.org",
    "docs.python.org",
    "pypi.org",
    "stackoverflow.com",
    "*.stackexchange.com",
    "wikipedia.org",
    "*.wikipedia.org",
    "readthedocs.io",
    "*.readthedocs.io",
)


def _load_allowlist_from_env() -> tuple[str, ...]:
    raw = os.environ.get("LUXE_BROWSER_ALLOWLIST")
    if not raw:
        return DEFAULT_BROWSER_ALLOWLIST
    return tuple(p.strip() for p in raw.split(",") if p.strip())


_ALLOWLIST: tuple[str, ...] = _load_allowlist_from_env()


def set_browser_allowlist(allowlist: tuple[str, ...]) -> None:
    global _ALLOWLIST
    _ALLOWLIST = tuple(allowlist)


def _allow_host(url: str) -> str | None:
    """Return None when the URL is allowed; an error message otherwise.
    Deny-by-default: no matching pattern (or an empty allowlist) refuses."""
    if not url or not url.startswith(("http://", "https://")):
        return "url must be absolute http(s)"
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return "url has no host"
    for pat in _ALLOWLIST:
        if fnmatch.fnmatch(host, pat.lower()):
            return None
    return (
        f"[denied] host '{host}' not in browser allowlist "
        f"({', '.join(_ALLOWLIST) if _ALLOWLIST else '<empty>'}). "
        "Override with LUXE_BROWSER_ALLOWLIST env var or "
        "set_browser_allowlist() in the REPL."
    )


# ── Chrome lifecycle ──────────────────────────────────────────────────

_chrome_proc: subprocess.Popen | None = None
_browser = None  # pychrome.Browser (lazy)
_tab = None     # pychrome.Tab    (lazy)


def _find_chrome_binary() -> str | None:
    """Locate a headless-capable Chromium binary. Caller decides what to
    do when None — typically return a clear install hint to the agent
    rather than raising."""
    candidates = (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    )
    for c in candidates:
        if "/" in c:
            if os.path.exists(c):
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    return None


def _ensure_chrome() -> tuple[Any, str | None]:
    """Lazy-launch Chrome + attach the CDP client. Returns (tab, None)
    on success or (None, error_message) on failure."""
    global _chrome_proc, _browser, _tab

    if _tab is not None and _chrome_proc and _chrome_proc.poll() is None:
        return _tab, None

    try:
        import pychrome  # noqa: F401
    except ImportError:
        return None, _MISSING_DEP_HINT

    binary = _find_chrome_binary()
    if not binary:
        return None, (
            "Chrome not found. Install with "
            "`brew install --cask google-chrome` (macOS) or your "
            "distro's chromium package."
        )

    # Tear down stale process before relaunch (e.g. Chrome crashed).
    if _chrome_proc is not None and _chrome_proc.poll() is not None:
        _chrome_proc = None
        _browser = None
        _tab = None

    if _chrome_proc is None:
        args = [
            binary,
            f"--remote-debugging-port={_CHROME_REMOTE_PORT}",
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--user-data-dir=" + os.path.expanduser("~/.luxe/chrome-profile"),
        ]
        try:
            _chrome_proc = subprocess.Popen(  # noqa: S603
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:  # noqa: BLE001
            return None, f"failed to launch Chrome: {type(e).__name__}: {e}"
        atexit.register(_kill_chrome)

    # Wait for the DevTools endpoint to come up.
    import pychrome
    deadline = time.monotonic() + 15.0
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            _browser = pychrome.Browser(url=f"http://127.0.0.1:{_CHROME_REMOTE_PORT}")
            tabs = _browser.list_tab()
            _tab = tabs[0] if tabs else _browser.new_tab()
            _tab.start()
            _tab.Page.enable()
            _tab.Runtime.enable()
            return _tab, None
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.3)

    # Couldn't reach DevTools — kill the subprocess so we don't leave
    # a zombie behind, and surface the original failure.
    _kill_chrome()
    return None, f"Chrome DevTools never came up: {last_err}"


def _kill_chrome() -> None:
    global _chrome_proc, _browser, _tab
    if _tab is not None:
        try:
            _tab.stop()
        except Exception:  # noqa: BLE001
            pass
    if _chrome_proc is not None:
        try:
            _chrome_proc.terminate()
            _chrome_proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                _chrome_proc.kill()
            except Exception:  # noqa: BLE001
                pass
    _chrome_proc = None
    _browser = None
    _tab = None


# ── Tool definitions ──────────────────────────────────────────────────


def tool_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name="browse_navigate",
            description=(
                "Load a URL in a real browser (Chrome, headless) and "
                "wait for JS to render. Use for pages plain fetching "
                "can't read (SPAs, dashboards, JS-rendered docs). After "
                "this returns, call `browse_read` to get the rendered "
                "text. Read-only: no click/fill/screenshot. Subject to "
                "a domain allowlist — disallowed hosts return [denied]."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute http(s) URL.",
                    },
                },
                "required": ["url"],
            },
        ),
        ToolDef(
            name="browse_read",
            description=(
                "Read the rendered DOM of the currently-loaded page "
                "as plain text (headers/navs/scripts stripped via "
                "trafilatura). Call after `browse_navigate`. Truncated "
                "to 12 KB."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
        ),
    ]


# ── Tool implementations ──────────────────────────────────────────────
# Error contract: ("", error_message) — luxe's ToolFn shape (tools.sdd).
# The ff6eab7 original returned (None, err); every site is normalized here.


def browse_navigate(args: dict[str, Any]) -> tuple[str, str | None]:
    url = (args.get("url") or "").strip()
    deny = _allow_host(url)
    if deny:
        return "", deny

    tab, err = _ensure_chrome()
    if err:
        return "", err

    try:
        tab.Page.navigate(url=url, _timeout=_NAV_TIMEOUT_S)
        # Wait for the load event. pychrome exposes wait for this.
        tab.wait(_NAV_TIMEOUT_S)
        # Pull final URL + title via Runtime.evaluate so we don't depend
        # on Page.frameNavigated event ordering.
        loc = tab.Runtime.evaluate(
            expression="window.location.href", returnByValue=True
        )
        title = tab.Runtime.evaluate(
            expression="document.title", returnByValue=True
        )
        final_url = (loc or {}).get("result", {}).get("value") or url
        page_title = (title or {}).get("result", {}).get("value") or ""
    except Exception as e:  # noqa: BLE001
        return "", f"navigation failed: {type(e).__name__}: {e}"

    return (
        json.dumps(
            {"url": url, "final_url": final_url, "title": page_title, "ok": True},
            ensure_ascii=False,
        ),
        None,
    )


def browse_read(args: dict[str, Any]) -> tuple[str, str | None]:
    if _tab is None:
        return "", "no page loaded — call browse_navigate first"

    try:
        import trafilatura
    except ImportError:
        return "", _MISSING_DEP_HINT

    try:
        result = _tab.Runtime.evaluate(
            expression="document.documentElement.outerHTML",
            returnByValue=True,
        )
        html = (result or {}).get("result", {}).get("value") or ""
        loc = _tab.Runtime.evaluate(
            expression="window.location.href", returnByValue=True
        )
        url = (loc or {}).get("result", {}).get("value") or ""
    except Exception as e:  # noqa: BLE001
        return "", f"page read failed: {type(e).__name__}: {e}"

    if not html:
        return "", "page returned empty HTML"

    text = trafilatura.extract(
        html, include_comments=False, include_tables=True, favor_recall=True,
    ) or ""
    text = text.strip()
    if not text:
        return "", "no readable content extracted from rendered page"

    truncated = len(text) > MAX_BROWSE_CHARS
    if truncated:
        text = text[:MAX_BROWSE_CHARS]
    return (
        json.dumps(
            {"url": url, "text": text, "truncated": truncated},
            ensure_ascii=False,
        ),
        None,
    )


def make_browser_tools() -> list[tuple[ToolDef, ToolFn]]:
    """(ToolDef, ToolFn) pairs for the chat extra-tool seam (same pattern as
    make_net_probe_tool). Each fn is wrapped so it NEVER raises — a tool
    exception becomes ("", "<Type>: <msg>") per the tools.sdd contract."""
    def _wrap(fn: ToolFn) -> ToolFn:
        def _inner(args: dict[str, Any]) -> tuple[str, str | None]:
            try:
                return fn(args or {})
            except Exception as e:  # a tool must never crash the loop
                return "", f"{type(e).__name__}: {e}"
        return _inner

    nav_def, read_def = tool_defs()
    return [(nav_def, _wrap(browse_navigate)),
            (read_def, _wrap(browse_read))]
