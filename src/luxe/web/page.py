"""Interactive page session — the drive-a-page half of the web surface.

`web_fetch(render=true)` READS a page; this module lets the model act on one:
open → read → click/type/scroll/back across a persistent page. It answers the
three questions that kept this deferred on 2026-08-03 (RESUME):

- **Who owns the browser between tool calls?** A single module-level
  `PageSession` whose Playwright driver lives on a dedicated daemon thread.
  Sync Playwright objects are bound to the thread that created them, and the
  TUI executes each turn on a worker thread that need not be the same one
  twice — so tool calls never touch the driver directly; they post an op to
  the owner thread's queue and wait, with a deadline, for the result. One
  page per process, started lazily on the first op.
- **What does a cancelled turn do to it?** Nothing. The caller abandons its
  wait; the owner thread finishes (or times out) the op in the background and
  the session remains usable. A caller-side deadline miss marks the session
  wedged and closes it, so a hung driver can never eat more than one turn.
- **The egress guard re-runs on every hop.** `_assert_public` runs before
  `open` AND after every action that can navigate (a click can go anywhere,
  JS can redirect). A violation HARD-CLOSES the session — private/tailnet
  space is a stop, not a detour. Popup windows are closed at birth: one page
  is the whole surface.

Chat-only via the extra-tool seam and `/web`-gated like the rest of
`luxe.web` (web.sdd). Never in `TOOL_FNS`.
"""

from __future__ import annotations

import atexit
import queue
import threading

from luxe.web.browser import (
    DEFAULT_RENDER_TIMEOUT_S,
    _browsers_root,
    _system_chrome,
    availability,
)
from luxe.web.fetch import WebError, _assert_public, _normalize_url

# Per-action driver timeout. Opens get the render ceiling; clicks/typing on a
# loaded page should be near-instant, so a short bound catches a dead page
# without eating the turn.
ACTION_TIMEOUT_S = 15.0
# Caller-side margin on top of the driver timeout before an op is declared
# wedged (driver timeouts normally fire first and return a clean error).
_OP_WAIT_MARGIN_S = 15.0
# Interactable listing cap: enough for real pages, small enough to stay
# readable inside a tool result.
_MAX_INTERACTABLES = 60

_ENUMERATE_JS = """
() => {
  const sel = 'a[href], button, input, textarea, select, [role="button"]';
  const els = Array.from(document.querySelectorAll(sel));
  const out = [];
  for (const e of els) {
    if (out.length >= %d) break;
    const text = (e.innerText || e.value || e.getAttribute('aria-label')
                  || e.getAttribute('placeholder') || '').trim();
    const i = out.length;
    e.setAttribute('data-luxe-i', String(i));
    out.push({i: i, tag: e.tagName.toLowerCase(),
              type: e.getAttribute('type') || '',
              href: e.getAttribute('href') || '',
              text: text.slice(0, 80)});
  }
  return out;
}
""" % _MAX_INTERACTABLES


def _default_launch():
    """Start Playwright and return (playwright, browser, page).

    Same chromium-or-system-Chrome resolution as `browser.render_url`. Runs
    ON the owner thread — sync Playwright must be used from the thread that
    started it.
    """
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    p = sync_playwright().start()
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
    context = browser.new_context(
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36 luxe/1.0"),
        viewport={"width": 1280, "height": 2000},
    )
    page = context.new_page()
    # One page is the whole surface: a popup would be an unguarded window.
    # Registered AFTER new_page and identity-guarded — the handler fires for
    # every page in the context, our own included (that closed the main page
    # at birth in the first cut; the live drill caught it).
    context.on("page",
               lambda extra: extra.close() if extra is not page else None)
    return p, browser, page


class PageSession:
    """One interactive page, owned by one daemon thread. See module docstring.

    `launch` is injectable for tests: it must return a triple whose last
    element duck-types the Playwright Page (goto/url/title/content/evaluate/
    click/fill/press/go_back) and whose first two have `.stop()`/`.close()`.
    """

    def __init__(self, launch=_default_launch):
        self._launch = launch
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._driver = None  # (playwright, browser, page) — owner thread only

    # -- caller side ---------------------------------------------------------

    def op(self, action: str, *, timeout_s: float | None = None,
           **kwargs) -> dict:
        """Run `action` on the owner thread and return its result dict.

        Raises WebError on any failure. A caller-deadline miss (a wedged
        driver) closes the session so the next op starts fresh.
        """
        with self._lock:
            if self._closed:
                raise WebError("page session is closed — use action=open")
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._worker, name="luxe-web-page", daemon=True)
                self._thread.start()
                atexit.register(self.close)
        budget = (timeout_s if timeout_s is not None else ACTION_TIMEOUT_S)
        out: queue.Queue = queue.Queue(maxsize=1)
        self._q.put((action, kwargs, budget, out))
        try:
            status, payload = out.get(timeout=budget + _OP_WAIT_MARGIN_S)
        except queue.Empty:
            self.close()
            raise WebError(
                f"web_page {action} did not finish within "
                f"{budget + _OP_WAIT_MARGIN_S:.0f}s; the page session was "
                "closed — action=open starts a fresh one") from None
        if status == "err":
            if isinstance(payload, WebError):
                raise payload
            raise WebError(f"web_page {action} failed: "
                           f"{type(payload).__name__}: {payload}")
        return payload

    def close(self) -> None:
        """Idempotent; safe from any thread, including atexit."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is not None and thread.is_alive():
            self._q.put(("__close__", {}, ACTION_TIMEOUT_S, None))
            thread.join(timeout=ACTION_TIMEOUT_S)

    # -- owner thread --------------------------------------------------------

    def _worker(self) -> None:
        while True:
            action, kwargs, budget, out = self._q.get()
            if action == "__close__":
                self._teardown()
                return
            try:
                result = self._dispatch(action, kwargs, budget)
                if out is not None:
                    out.put(("ok", result))
            except BaseException as e:  # noqa: BLE001 — must reach the caller
                if out is not None:
                    out.put(("err", e))

    def _teardown(self) -> None:
        if self._driver is None:
            return
        p, browser, _page = self._driver
        self._driver = None
        for closer in (browser.close, p.stop):
            try:
                closer()
            except Exception:
                pass

    def _page(self):
        if self._driver is None:
            if self._launch is _default_launch:
                # Only the real driver has installability preconditions; an
                # injected launcher (tests) answers for itself.
                avail = availability()
                if not avail.ok:
                    raise WebError(
                        f"cannot open a page: {avail.reason}. Fix: {avail.fix}")
            self._driver = self._launch()
        return self._driver[2]

    def _guard(self, page) -> None:
        """Egress check on the CURRENT url; violation hard-closes the session."""
        try:
            _assert_public(page.url)
        except WebError:
            self._teardown()
            with self._lock:
                self._closed = True
            raise

    def _dispatch(self, action: str, kwargs: dict, budget: float) -> dict:
        timeout_ms = int(budget * 1000)
        if action == "open":
            url = _normalize_url(str(kwargs.get("url") or "").strip())
            _assert_public(url)
            page = self._page()
            page.goto(url, timeout=int(DEFAULT_RENDER_TIMEOUT_S * 1000),
                      wait_until="domcontentloaded")
        elif action == "read":
            page = self._require_page()
        elif action == "click":
            page = self._require_page()
            page.click(self._selector(kwargs), timeout=timeout_ms)
            self._settle(page)
        elif action == "type":
            page = self._require_page()
            sel = self._selector(kwargs)
            page.fill(sel, str(kwargs.get("text") or ""), timeout=timeout_ms)
            if kwargs.get("submit"):
                page.press(sel, "Enter", timeout=timeout_ms)
                self._settle(page)
        elif action == "scroll":
            page = self._require_page()
            delta = -1600 if str(kwargs.get("direction") or "down") == "up" else 1600
            page.evaluate(f"() => window.scrollBy(0, {delta})")
        elif action == "back":
            page = self._require_page()
            page.go_back(timeout=timeout_ms, wait_until="domcontentloaded")
        else:
            raise WebError(f"web_page: unknown action {action!r}")

        self._guard(page)
        return self._snapshot(page)

    def _require_page(self):
        if self._driver is None:
            raise WebError("no page is open — use action=open first")
        return self._driver[2]

    @staticmethod
    def _selector(kwargs: dict) -> str:
        """`target` is an index from the interactable listing or a CSS selector."""
        target = str(kwargs.get("target") or "").strip()
        if not target:
            raise WebError("web_page: `target` is required — an index from "
                           "the interactable listing, or a CSS selector")
        if target.isdigit():
            return f'[data-luxe-i="{int(target)}"]'
        return target

    def _settle(self, page) -> None:
        """Best-effort wait for a click/submit-triggered navigation to land."""
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass

    def _snapshot(self, page) -> dict:
        try:
            interactables = page.evaluate(_ENUMERATE_JS) or []
        except Exception:
            interactables = []
        return {
            "url": page.url,
            "title": page.title() or "",
            "html": page.content(),
            "interactables": interactables,
        }


# -- module surface (the tool layer talks to these) ---------------------------

_SESSION: PageSession | None = None
_SESSION_LOCK = threading.Lock()


def get_session() -> PageSession:
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None or _SESSION._closed:
            _SESSION = PageSession()
        return _SESSION


def close_session() -> None:
    """Close the process-wide session if any (atexit, `/web` off, action=close)."""
    global _SESSION
    with _SESSION_LOCK:
        session, _SESSION = _SESSION, None
    if session is not None:
        session.close()


def render_snapshot(snap: dict, *, max_chars: int = 12_000) -> str:
    """Format an op result for the model: state line, content, interactables."""
    from luxe.web.extract import to_markdown
    from luxe.web.fetch import FetchResult

    body = to_markdown(
        FetchResult(url=snap["url"], status=200,
                    content_type="text/html; charset=utf-8",
                    text=snap["html"], elapsed_s=0.0),
        max_chars=max_chars)
    lines = [f"page: {snap['title'] or '(untitled)'} — {snap['url']}", "", body]
    if snap["interactables"]:
        lines += ["", "interactable elements (click/type by index):"]
        for el in snap["interactables"]:
            desc = f"[{el['i']}] {el['tag']}"
            if el.get("type"):
                desc += f"({el['type']})"
            if el.get("text"):
                desc += f' "{el["text"]}"'
            if el.get("href"):
                desc += f" → {el['href'][:100]}"
            lines.append(desc)
        if len(snap["interactables"]) >= _MAX_INTERACTABLES:
            # The enumerator stops at _MAX_INTERACTABLES inside the page's own
            # JS, so a listing of exactly that length is a listing that may
            # have been cut — and an uncut listing was the model's only way to
            # conclude "that control isn't on this page". Same rule as grep and
            # read_file: say the cut, then say the way around it.
            lines.append(f"[listing capped at {_MAX_INTERACTABLES} elements — "
                         f"there may be more; scroll, or use a CSS selector as "
                         f"`target` instead of an index]")
    return "\n".join(lines)
