"""Tests for the chat-only web tools: egress guard, extraction, gating.

Network is never touched. The fetch tests drive a real local HTTP server (the
netdiag pattern) with `LUXE_WEB_ALLOW_PRIVATE=1`, so the transport is
exercised for real while the guard itself is tested separately against the
addresses it must refuse.
"""

from __future__ import annotations

import http.server
import threading

import pytest

from luxe.web import answers as answers_mod
from luxe.web import extract as extract_mod
from luxe.web import search as search_mod
from luxe.web.extract import extract_text, to_markdown
from luxe.web.fetch import FetchResult, WebError, _is_public_ip, fetch_url
from luxe.web.tools import (make_web_answer_tool, make_web_fetch_tool,
                            make_web_search_tool, web_tools)


# --- egress guard -----------------------------------------------------------
#
# This is the security boundary: luxe runs beside privileged tailnet relays,
# a localhost oMLX endpoint and a NAS, so an ungated fetch tool is SSRF.

@pytest.mark.parametrize("ip", [
    "127.0.0.1",        # loopback — the oMLX endpoint lives here
    "::1",
    "10.0.0.5",         # RFC1918 — the LAN / NAS
    "192.168.1.248",    # kappa
    "100.64.94.86",     # CGNAT range — the tailnet
    "169.254.169.254",  # cloud metadata
    "0.0.0.0",
    "224.0.0.1",        # multicast
])
def test_non_public_addresses_are_refused(ip):
    assert _is_public_ip(ip) is False


@pytest.mark.parametrize("ip", ["1.1.1.1", "93.184.216.34", "2606:4700::1111"])
def test_public_addresses_are_allowed(ip):
    assert _is_public_ip(ip) is True


def test_fetch_refuses_loopback_by_default(monkeypatch):
    monkeypatch.delenv("LUXE_WEB_ALLOW_PRIVATE", raising=False)
    with pytest.raises(WebError) as e:
        fetch_url("http://127.0.0.1:9/nope")
    assert "non-public" in str(e.value)


def test_fetch_refuses_non_http_schemes(monkeypatch):
    monkeypatch.delenv("LUXE_WEB_ALLOW_PRIVATE", raising=False)
    for url in ("file:///etc/passwd", "ftp://example.com/x", "data:text/html,hi"):
        with pytest.raises(WebError) as e:
            fetch_url(url)
        assert "scheme" in str(e.value)


def test_guard_resolves_names_not_patterns(monkeypatch):
    """A public NAME that resolves to a private address must still be refused —
    the reason the guard resolves instead of string-matching."""
    monkeypatch.delenv("LUXE_WEB_ALLOW_PRIVATE", raising=False)
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 80))])
    with pytest.raises(WebError) as e:
        fetch_url("http://totally-public-name.example/")
    assert "127.0.0.1" in str(e.value)


def test_no_allowlist_means_any_public_host(monkeypatch):
    """Unset LUXE_WEB_ALLOWLIST ⇒ no host restriction (IP guard still runs)."""
    monkeypatch.delenv("LUXE_WEB_ALLOWLIST", raising=False)
    from luxe.web.fetch import _host_allowlist
    assert _host_allowlist() == ()


def test_allowlist_when_set_is_deny_by_default(monkeypatch):
    monkeypatch.delenv("LUXE_WEB_ALLOW_PRIVATE", raising=False)
    monkeypatch.setenv("LUXE_WEB_ALLOWLIST", "docs.python.org, *.github.com")
    from luxe.web.fetch import _assert_public

    _assert_public("https://docs.python.org/3/")       # exact
    _assert_public("https://api.github.com/repos")     # glob
    with pytest.raises(WebError) as e:
        _assert_public("https://example.com/")
    assert "LUXE_WEB_ALLOWLIST" in str(e.value)


def test_allowlist_does_not_replace_the_ip_guard(monkeypatch):
    """An allowlisted NAME that resolves privately is still refused.

    The inherited allowlist-only design could not express this: it checks the
    hostname and never asks where it points.
    """
    monkeypatch.delenv("LUXE_WEB_ALLOW_PRIVATE", raising=False)
    monkeypatch.setenv("LUXE_WEB_ALLOWLIST", "*.internal.test")
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("100.89.62.17", 443))])
    from luxe.web.fetch import _assert_public
    with pytest.raises(WebError) as e:
        _assert_public("https://kappa.internal.test/")
    assert "100.89.62.17" in str(e.value)


def test_tool_argument_cannot_lift_the_guard(monkeypatch):
    """The escape hatch is an env var by design; no tool arg may reach it."""
    monkeypatch.delenv("LUXE_WEB_ALLOW_PRIVATE", raising=False)
    _defn, fn = make_web_fetch_tool()
    for args in ({"url": "http://127.0.0.1/"},
                 {"url": "http://127.0.0.1/", "allow_private": True}):
        out, err = fn(args)
        assert out == "" and err and "non-public" in err


# --- fetch against a real local server --------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        if self.path == "/json":
            body = b'{"ok": true, "n": 1}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/big":
            body = b"x" * 500_000
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        else:
            body = (b"<html><head><title>Doc</title></head><body>"
                    b"<nav>skip me</nav><h1>Heading</h1><p>Hello world.</p>"
                    b"<pre>code_here()</pre>"
                    b"<a href='/next'>Next page</a>"
                    b"<script>var x = 'nope';</script></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _FastServer(http.server.HTTPServer):
    """HTTPServer without the reverse-DNS stall.

    Stock `server_bind` calls `socket.getfqdn()`, which blocked ~35s per
    session on a machine whose resolver is slow for reverse lookups. The
    hostname it computes is never used here.
    """

    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("LUXE_WEB_ALLOW_PRIVATE", "1")
    srv = _FastServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_fetch_html_and_extract(server):
    r = fetch_url(f"{server}/page")
    assert r.status == 200 and r.is_html
    title, text, links = extract_text(r.text, base_url=r.url)
    assert title == "Doc"
    assert "Hello world." in text
    assert "# Heading" in text
    assert "code_here()" in text
    assert "nope" not in text          # <script> dropped
    assert "skip me" not in text       # <nav> chrome dropped
    assert any(href.endswith("/next") for _t, href in links)


def test_fetch_follows_redirect_and_guards_each_hop(server):
    r = fetch_url(f"{server}/redirect")
    assert r.status == 200
    assert r.redirects and r.url.endswith("/page")


def test_non_html_passes_through_unreformatted(server):
    r = fetch_url(f"{server}/json")
    out = to_markdown(r)
    assert '{"ok": true, "n": 1}' in out


def test_byte_cap_truncates(server):
    r = fetch_url(f"{server}/big", max_bytes=1000)
    assert r.truncated and len(r.text) <= 1000


def test_fetch_tool_returns_text_not_exception(server):
    _defn, fn = make_web_fetch_tool()
    out, err = fn({"url": f"{server}/page"})
    assert err is None and "Hello world." in out


def test_fetch_tool_requires_url():
    _defn, fn = make_web_fetch_tool()
    out, err = fn({})
    assert out == "" and "url" in err


# --- extraction edge cases --------------------------------------------------

def test_short_page_without_scripts_is_not_called_js_rendered():
    """example.com extracts ~150 chars and is not a SPA — the note must not
    fire merely because a page is short."""
    r = FetchResult(url="https://x.test/", status=200,
                    content_type="text/html",
                    text="<html><body><h1>Hi</h1><p>Short page.</p></body></html>")
    assert "render=true" not in to_markdown(r)


def test_js_only_page_says_so_instead_of_looking_empty():
    r = FetchResult(url="https://x.test/", status=200,
                    content_type="text/html",
                    text="<html><body><div id='root'></div>"
                         "<script>render()</script></body></html>")
    out = to_markdown(r)
    assert "render=true" in out and "JavaScript" in out


def test_malformed_html_degrades_instead_of_raising():
    title, text, _links = extract_text("<p>unclosed <b>bold <div>x")
    assert isinstance(text, str)


def test_max_chars_is_reported_when_truncating():
    r = FetchResult(url="https://x.test/", status=200,
                    content_type="text/html",
                    text="<html><body><p>" + ("word " * 5000) + "</p></body></html>")
    out = to_markdown(r, max_chars=200)
    assert "truncated" in out and len(out) < 600


def test_tidy_collapses_blank_lines():
    assert "\n\n\n" not in extract_mod._tidy("a\n\n\n\n\nb")


# --- search gating ----------------------------------------------------------

def test_search_withheld_without_a_key(monkeypatch):
    monkeypatch.setattr(search_mod, "active_provider", lambda: None)
    monkeypatch.setattr(answers_mod, "_key", lambda: "")
    defs, fns = web_tools()
    names = [d.name for d in defs]
    assert "web_fetch" in names
    assert "web_search" not in names and "web_search" not in fns
    assert "web_answer" not in names and "web_answer" not in fns


def test_search_included_when_a_key_resolves(monkeypatch):
    monkeypatch.setattr(search_mod, "active_provider",
                        lambda: (search_mod._PROVIDERS[0], "k"))
    monkeypatch.setattr(search_mod, "configured", lambda: True)
    defs, _fns = web_tools()
    assert "web_search" in [d.name for d in defs]


def test_search_tool_reports_missing_key_as_a_tool_error(monkeypatch):
    monkeypatch.setattr(search_mod, "active_provider", lambda: None)
    _defn, fn = make_web_search_tool()
    out, err = fn({"query": "anything"})
    assert out == "" and "API key" in err


def test_render_hits_formats_results():
    hits = [search_mod.SearchHit(title="T", url="https://u.test", snippet="S")]
    out = search_mod.render_hits("brave", "q", hits)
    assert "https://u.test" in out and "web_fetch" in out


# --- answers (a separate product from search, gated on its own key) ---------

def test_answers_gated_independently_of_search(monkeypatch):
    """Search key present, answers key absent ⇒ web_search yes, web_answer no
    — and vice versa. They are separate subscriptions (web.sdd)."""
    monkeypatch.setattr(search_mod, "active_provider",
                        lambda: (search_mod._PROVIDERS[0], "k"))
    monkeypatch.setattr(search_mod, "configured", lambda: True)
    monkeypatch.setattr(answers_mod, "_key", lambda: "")
    names = [d.name for d in web_tools()[0]]
    assert "web_search" in names and "web_answer" not in names

    monkeypatch.setattr(search_mod, "active_provider", lambda: None)
    monkeypatch.setattr(search_mod, "configured", lambda: False)
    monkeypatch.setattr(answers_mod, "_key", lambda: "ans-key")
    names = [d.name for d in web_tools()[0]]
    assert "web_search" not in names and "web_answer" in names


def test_answer_tool_reports_missing_key_as_a_tool_error(monkeypatch):
    monkeypatch.setattr(answers_mod, "_key", lambda: "")
    _defn, fn = make_web_answer_tool()
    out, err = fn({"query": "anything"})
    assert out == "" and "API key" in err


def test_answer_parses_openai_shape(monkeypatch):
    import httpx

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"model": "brave-pro",
                    "choices": [{"message": {"role": "assistant",
                                             "content": "K2."}}]}

    monkeypatch.setattr(answers_mod, "_key", lambda: "ans-key")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    out = answers_mod.answer("second highest mountain?")
    assert "K2." in out and "brave-pro" in out


def test_answer_rejects_unknown_model(monkeypatch):
    monkeypatch.setattr(answers_mod, "_key", lambda: "ans-key")
    with pytest.raises(WebError) as e:
        answers_mod.answer("q", model="gpt-9")
    assert "brave" in str(e.value)


def test_answer_empty_content_is_an_error_not_a_blank_success(monkeypatch):
    import httpx

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": ""}}]}

    monkeypatch.setattr(answers_mod, "_key", lambda: "ans-key")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(WebError) as e:
        answers_mod.answer("q")
    assert "empty" in str(e.value)


# --- chat gating ------------------------------------------------------------
#
# The load-bearing guarantee: web tools reach a turn ONLY via the extra-tool
# seam, and only when /web is on. The benchmark path passes no extra tools.

def test_web_tools_are_absent_from_the_benchmark_tool_surface():
    """The contract: a maintain/bench run must not be able to call the web.

    `build_tools` is what the benchmark path assembles; extra tools arrive
    only through run_single's seam, which that path never populates.
    """
    from luxe.agents.single import _build_full_tool_surface

    for task_type in ("implement", "bugfix", "manage", "review"):
        defs, fns, _cacheable = _build_full_tool_surface(
            frozenset({"python"}), None, task_type)
        names = {d.name for d in defs} | set(fns)
        assert "web_fetch" not in names, task_type
        assert "web_search" not in names, task_type
        assert "web_answer" not in names, task_type


def test_session_web_defaults_off():
    from luxe.chat.session import ChatSession

    assert ChatSession(repo_path="", project_hash="").web_enabled is False


def test_web_command_toggles_the_flag():
    from luxe.chat import commands as cmd

    assert "/web" in cmd._build_handlers()
