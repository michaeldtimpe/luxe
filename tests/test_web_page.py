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
