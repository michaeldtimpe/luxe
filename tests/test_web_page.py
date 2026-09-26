"""Interactive page session (`web_page`) — driver always faked, no browser.

The real Playwright layer is exercised by the manual drill in the 2026-08-05
RESUME handoff; CI must pass on hosts with no Chromium and no [web] extra
beyond playwright's absence, so every test injects a fake driver triple.
"""

from __future__ import annotations

import threading

import pytest

from luxe.web import page as page_mod
from luxe.web.fetch import WebError
from luxe.web.page import PageSession

# No live DNS: every hostname answers a public address (conftest).
pytestmark = pytest.mark.usefixtures("stub_public_dns")


class _FakePage:
    """Duck-types the slice of Playwright's Page that PageSession touches."""

    def __init__(self):
        self.url = "https://example.com/"
        self._title = "Example"
        self._html = "<html><body><h1>Example</h1><p>hello</p></body></html>"
        self.calls: list[tuple] = []
        self.next_url_after_click: str | None = None
        self.interactables = [
            {"i": 0, "tag": "a", "type": "", "href": "/more", "text": "More"},
            {"i": 1, "tag": "input", "type": "email", "href": "",
             "text": "Email address"},
        ]

    def goto(self, url, **kw):
        self.calls.append(("goto", url))
        self.url = url

    def title(self):
        return self._title

    def content(self):
        return self._html

    def evaluate(self, js):
        self.calls.append(("evaluate",))
        if "scrollBy" in js:
            return None
        return self.interactables

    def click(self, selector, **kw):
        self.calls.append(("click", selector))
        if self.next_url_after_click:
            self.url = self.next_url_after_click

    def fill(self, selector, text, **kw):
        self.calls.append(("fill", selector, text))

    def press(self, selector, key, **kw):
        self.calls.append(("press", selector, key))

    def go_back(self, **kw):
        self.calls.append(("go_back",))

    def wait_for_load_state(self, *a, **kw):
        pass


class _Stoppable:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True

    def close(self):
        self.stopped = True


def _session():
    fake = _FakePage()
    pw, browser = _Stoppable(), _Stoppable()
    s = PageSession(launch=lambda: (pw, browser, fake))
    return s, fake, pw, browser


class TestOwnership:
    def test_ops_from_different_threads_share_one_driver(self):
        """The whole point of the owner thread: the TUI's per-turn worker
        threads must be able to take turns driving the same page."""
        s, fake, *_ = _session()
        results = {}

        def _do(name, action, **kw):
            results[name] = s.op(action, **kw)

        t1 = threading.Thread(target=_do, args=("a", "open"),
                              kwargs={"url": "https://example.com/"})
        t1.start(); t1.join()
        t2 = threading.Thread(target=_do, args=("b", "read"))
        t2.start(); t2.join()
        assert results["a"]["url"] == "https://example.com/"
        assert results["b"]["title"] == "Example"
        s.close()

    def test_close_is_idempotent_and_stops_the_driver(self):
        s, _fake, pw, browser = _session()
        s.op("open", url="https://example.com/")
        s.close()
        s.close()
        assert pw.stopped and browser.stopped
        with pytest.raises(WebError, match="closed"):
            s.op("read")


class TestEgress:
    def test_open_refuses_non_public_before_touching_the_driver(self):
        launched = []
        s = PageSession(launch=lambda: launched.append(1) or None)
        with pytest.raises(WebError):
            s.op("open", url="http://127.0.0.1:8000/admin")
        assert launched == []  # guard fired before any browser existed
        s.close()

    def test_navigation_to_private_space_hard_closes_the_session(self):
        """A click can go anywhere; the guard re-runs AFTER every action and
        a violation is a stop, not a detour."""
        s, fake, pw, browser = _session()
        s.op("open", url="https://example.com/")
        fake.next_url_after_click = "http://127.0.0.1:8000/admin"
        with pytest.raises(WebError):
            s.op("click", target="0")
        assert pw.stopped and browser.stopped
        with pytest.raises(WebError, match="closed"):
            s.op("read")


class TestActions:
    def test_click_by_index_uses_the_tagged_selector(self):
        s, fake, *_ = _session()
        s.op("open", url="https://example.com/")
        s.op("click", target="0")
        assert ("click", '[data-luxe-i="0"]') in fake.calls
        s.close()

    def test_click_by_css_selector_passes_through(self):
        s, fake, *_ = _session()
        s.op("open", url="https://example.com/")
        s.op("click", target="a.nav")
        assert ("click", "a.nav") in fake.calls
        s.close()

    def test_type_with_submit_presses_enter(self):
        s, fake, *_ = _session()
        s.op("open", url="https://example.com/")
        s.op("type", target="1", text="a@b.c", submit=True)
        assert ("fill", '[data-luxe-i="1"]', "a@b.c") in fake.calls
        assert ("press", '[data-luxe-i="1"]', "Enter") in fake.calls
        s.close()

    def test_actions_before_open_are_a_clean_error(self):
        s, *_ = _session()
        with pytest.raises(WebError, match="action=open"):
            s.op("read")
        s.close()

    def test_missing_target_is_a_clean_error(self):
        s, *_ = _session()
        s.op("open", url="https://example.com/")
        with pytest.raises(WebError, match="target"):
            s.op("click")
        s.close()


class TestRendering:
    def test_snapshot_renders_state_content_and_interactables(self):
        s, *_ = _session()
        snap = s.op("open", url="https://example.com/")
        out = page_mod.render_snapshot(snap)
        assert "Example — https://example.com/" in out
        assert "hello" in out
        assert '[0] a "More" → /more' in out
        assert '[1] input(email) "Email address"' in out
        s.close()

    def test_a_full_listing_says_it_may_have_been_cut(self):
        """`_MAX_INTERACTABLES` is applied inside the page's own JS and was
        never mentioned in the snapshot, so a listing of exactly 60 elements
        looked like the whole page — and "that control isn't here" was the
        conclusion the model could draw from it."""
        s, fake, *_ = _session()
        fake.interactables = [
            {"i": i, "tag": "a", "type": "", "href": f"/{i}", "text": f"link {i}"}
            for i in range(page_mod._MAX_INTERACTABLES)
        ]
        out = page_mod.render_snapshot(s.op("open", url="https://example.com/"))
        assert f"capped at {page_mod._MAX_INTERACTABLES} elements" in out
        assert "CSS selector" in out
        s.close()

    def test_a_short_listing_is_unannotated(self):
        s, *_ = _session()
        out = page_mod.render_snapshot(s.op("open", url="https://example.com/"))
        assert "capped at" not in out
        s.close()


class TestToolSurface:
    def test_web_page_withheld_without_a_browser(self, monkeypatch):
        from luxe.web import browser as browser_mod
        from luxe.web import tools as tools_mod
        monkeypatch.setattr(
            browser_mod, "availability",
            lambda: browser_mod.Availability(ok=False, reason="x", fix="y"))
        defs, fns = tools_mod.web_tools(include_search=False)
        assert "web_page" not in fns
        assert [d.name for d in defs] == ["web_fetch"]

    def test_web_page_tool_close_never_needs_a_session(self):
        from luxe.web.tools import make_web_page_tool
        _d, fn = make_web_page_tool()
        out, err = fn({"action": "close"})
        assert err is None and "closed" in out

    def test_tool_description_carries_the_explicit_ask_rule(self):
        from luxe.web.tools import make_web_page_tool
        d, _fn = make_web_page_tool()
        s = d.description
        assert "NEVER submit" in s and "explicitly asked" in s
        assert "web_fetch" in s  # steers plain reading to the fast path


class _SlowGotoPage(_FakePage):
    def __init__(self, delay: float):
        super().__init__()
        self.delay = delay

    def goto(self, url, **kw):
        import time
        time.sleep(self.delay)
        super().goto(url, **kw)


class TestTimeouts:
    def test_open_waits_longer_than_the_navigation_it_started(self, monkeypatch):
        """The caller used to give up at ACTION_TIMEOUT_S + margin (30s)
        while goto itself was allowed 45s, so a slow-but-healthy page was
        declared wedged and hard-closed. Scaled down: action 0.05s, margin
        0.05s, render ceiling 1s, and a page that takes 0.3s to load."""
        monkeypatch.setattr(page_mod, "ACTION_TIMEOUT_S", 0.05)
        monkeypatch.setattr(page_mod, "_OP_WAIT_MARGIN_S", 0.05)
        monkeypatch.setattr(page_mod, "DEFAULT_RENDER_TIMEOUT_S", 1.0)
        fake = _SlowGotoPage(0.3)
        s = PageSession(launch=lambda: (_Stoppable(), _Stoppable(), fake))
        snap = s.op("open", url="https://example.com/")
        assert snap["url"] == "https://example.com/"
        s.close()

    def test_open_hands_the_browser_the_reserialized_url(self):
        s, fake, *_ = _session()
        s.op("open", url="HTTPS://EXAMPLE.com/a%20b")
        assert ("goto", "https://example.com/a%20b") in fake.calls
        s.close()


class _Guard:
    def __init__(self):
        self.escaped: list[str] = []

    def check(self):
        if self.escaped:
            raise WebError(f"refused: redirect to {self.escaped[0]}")


class TestLifecycle:
    def test_egress_close_ends_the_owner_thread(self):
        """A guard violation closed the session but left its daemon thread
        blocked on the queue forever — one leaked thread per violation."""
        s, fake, *_ = _session()
        s.op("open", url="https://example.com/")
        thread = s._thread
        fake.next_url_after_click = "http://127.0.0.1:8000/admin"
        with pytest.raises(WebError):
            s.op("click", target="0")
        thread.join(timeout=2)
        assert not thread.is_alive()

    def test_sessions_do_not_register_their_own_atexit(self, monkeypatch):
        import atexit
        registered = []
        monkeypatch.setattr(atexit, "register",
                            lambda fn, *a, **k: registered.append(fn))
        for _ in range(3):
            s, *_ = _session()
            s.op("open", url="https://example.com/")
            s.close()
        assert registered == []

    def test_a_redirect_hop_into_private_space_hard_closes(self):
        """Playwright routes only the first URL of a redirect chain; the
        guard records later hops and the session treats one as a stop."""
        fake, guard = _FakePage(), _Guard()
        pw, browser = _Stoppable(), _Stoppable()
        s = PageSession(launch=lambda: (pw, browser, fake, guard))
        s.op("open", url="https://example.com/")
        guard.escaped.append("http://10.0.0.1/")
        with pytest.raises(WebError, match="10.0.0.1"):
            s.op("click", target="0")
        assert pw.stopped and browser.stopped
        with pytest.raises(WebError, match="closed"):
            s.op("read")


# --- the in-browser egress guard (browser.EgressGuard) -----------------------

class _Req:
    def __init__(self, url, redirected_from=None):
        self.url = url
        self.redirected_from = redirected_from


class _Route:
    def __init__(self, url, fetch_error=False):
        self.request = _Req(url)
        self.outcome = None
        self.fetch_kw = None
        self._fetch_error = fetch_error

    def abort(self, code=None):
        self.outcome = ("abort", code)

    def fetch(self, **kw):
        self.fetch_kw = kw
        if self._fetch_error:
            raise RuntimeError("net down")
        return "RESP"

    def fulfill(self, response=None):
        self.outcome = ("fulfill", response)


class _WS:
    def __init__(self, url):
        self.url = url
        self.outcome = None

    def close(self, code=None, reason=None):
        self.outcome = ("close", code)

    def connect_to_server(self):
        self.outcome = ("connect",)


class _Ctx:
    def __init__(self):
        self.routes, self.ws_routes, self.events = [], [], []

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def route_web_socket(self, pattern, handler):
        self.ws_routes.append((pattern, handler))

    def on(self, event, handler):
        self.events.append((event, handler))


class TestBrowserEgressGuard:
    def test_subresources_to_private_space_are_aborted(self):
        from luxe.web.browser import EgressGuard
        g = EgressGuard()
        for url in ("http://127.0.0.1:8000/v1/models",
                    "http://100.89.62.17/mcp",
                    "http://169.254.169.254/latest/meta-data/",
                    "http://0177.0.0.1/",
                    "file:///etc/passwd"):
            r = _Route(url)
            g.on_route(r)
            assert r.outcome == ("abort", "blockedbyclient"), url
        ok = _Route("https://example.com/app.js")
        g.on_route(ok)
        assert ok.outcome == ("fulfill", "RESP")
        # Redirects come back to the browser as a NEW (routed) request.
        assert ok.fetch_kw == {"max_redirects": 0}
        down = _Route("https://example.com/x", fetch_error=True)
        g.on_route(down)
        assert down.outcome == ("abort", "failed")

    def test_websockets_are_guarded(self):
        from luxe.web.browser import EgressGuard
        g = EgressGuard()
        bad, good = _WS("ws://127.0.0.1:8000/"), _WS("wss://example.com/s")
        g.on_websocket(bad)
        g.on_websocket(good)
        assert bad.outcome is None   # never connected to its server
        assert good.outcome == ("connect",)

    def test_a_response_from_private_space_is_recorded_and_raises(self):
        from luxe.web.browser import EgressGuard
        g = EgressGuard()
        g.on_response(_Req("https://example.com/"))
        assert g.escaped == []
        g.on_response(_Req("http://127.0.0.1/x"))
        assert g.escaped == ["http://127.0.0.1/x"]
        with pytest.raises(WebError, match="non-public"):
            g.check()

    def test_launch_blocks_service_workers_and_installs_every_hook(self):
        from luxe.web import browser as browser_mod
        ctx, seen = _Ctx(), {}

        class _Browser:
            def new_context(self, **kw):
                seen.update(kw)
                return ctx

            def close(self):
                pass

        class _Chromium:
            def launch(self, **kw):
                return _Browser()

        class _P:
            chromium = _Chromium()

        _b, got_ctx, guard = browser_mod.launch_guarded(_P())
        assert got_ctx is ctx and seen["service_workers"] == "block"
        assert [p for p, _h in ctx.routes] == ["**/*"]
        assert [p for p, _h in ctx.ws_routes] == ["**/*"]
        assert ("response", guard.on_response) in ctx.events
