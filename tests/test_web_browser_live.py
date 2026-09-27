"""Real-Chromium regression for the in-browser egress guard.

Loopback only: a "public" server on 127.0.0.1 (declared public for the test
by patching `_is_public_ip`) and a "private" server on [::1] that must never
receive a request. Skipped when no browser can launch. The assertion that
matters is ZERO hits on the private server — an error raised after the
browser already sent the GET is not a pass.
"""

from __future__ import annotations

import http.server
import socket
import socketserver
import threading

import pytest

from luxe.web import browser as browser_mod
from luxe.web import fetch as fetch_mod
from luxe.web.fetch import WebError

pytestmark = pytest.mark.skipif(not browser_mod.availability().ok,
                                reason="no Chromium / playwright")


def _serve(family, host, handler):
    class _S(socketserver.ThreadingMixIn, http.server.HTTPServer):
        address_family = family
        daemon_threads = True

        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            self.server_name = "x"
            self.server_port = self.server_address[1]

    srv = _S((host, 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def servers(monkeypatch):
    real = fetch_mod._is_public_ip
    monkeypatch.setattr(fetch_mod, "_is_public_ip",
                        lambda ip: ip == "127.0.0.1" or real(ip))
    hits: list[str] = []

    class Private(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            body = b"SECRET"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    priv = _serve(socket.AF_INET6, "::1", Private)
    secret = f"http://[::1]:{priv.server_port}"

    class Public(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            routes = {"/to-private": f"{secret}/top-level",
                      "/hop": "/to-private",            # public → public → private
                      "/to-public": "/page"}
            if self.path in routes:
                self.send_response(302)
                self.send_header("Location", routes[self.path])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/iframe":
                body = b"<html><body><iframe src='/to-private'></iframe></body></html>"
            else:
                body = b"<html><body><p>PUBLIC-PAGE</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    pub = _serve(socket.AF_INET, "127.0.0.1", Public)
    yield f"http://127.0.0.1:{pub.server_port}", hits
    pub.shutdown()
    priv.shutdown()


@pytest.mark.parametrize("path", ["/to-private", "/hop"])
def test_top_level_redirect_into_private_space_sends_nothing(servers, path):
    base, hits = servers
    with pytest.raises(WebError):
        browser_mod.render_url(base + path, timeout_s=20)
    assert hits == []


def test_iframe_redirect_into_private_space_sends_nothing(servers):
    base, hits = servers
    try:
        browser_mod.render_url(base + "/iframe", timeout_s=20)
    except WebError:
        pass
    assert hits == []


def test_a_public_redirect_still_lands(servers):
    base, hits = servers
    final, _title, html = browser_mod.render_url(base + "/to-public",
                                                 timeout_s=20)
    assert final.endswith("/page") and "PUBLIC-PAGE" in html
    assert hits == []
