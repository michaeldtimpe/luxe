"""Egress guard: one parser, one resolution, and a connect pinned to it.

Hermetic: every name is answered by a stubbed `socket.getaddrinfo`, and the
only sockets opened are to a local HTTP server on 127.0.0.1. To exercise the
guard (rather than lift it with LUXE_WEB_ALLOW_PRIVATE) the tests declare
127.0.0.1 "public" via `_is_public_ip`, so a name can resolve to the local
server and still be checked for real.
"""

from __future__ import annotations

import gzip
import http.server
import ipaddress
import socket
import threading
import time

import pytest

from luxe.web import fetch as fetch_mod
from luxe.web.fetch import WebError, fetch_url, resolve_public

_REAL_GAI = socket.getaddrinfo


def _gai_answer(ip: str, port: int):
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    addr = (ip, port, 0, 0) if fam == socket.AF_INET6 else (ip, port)
    return [(fam, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", addr)]


class _DNS:
    """Name → list of answers (consumed in order; last one repeats)."""

    def __init__(self):
        self.answers: dict[str, list[str]] = {}
        self.calls: dict[str, int] = {}

    def __call__(self, host, port, *a, **k):
        h = host.decode() if isinstance(host, bytes) else str(host)
        try:
            ipaddress.ip_address(h)
            return _REAL_GAI(host, port, *a, **k)   # literals: no DNS
        except ValueError:
            pass
        self.calls[h] = self.calls.get(h, 0) + 1
        seq = self.answers.get(h) or ["93.184.216.34"]
        ip = seq[min(self.calls[h], len(seq)) - 1]
        return _gai_answer(ip, int(port or 80))


@pytest.fixture()
def dns(monkeypatch):
    d = _DNS()
    monkeypatch.setattr(socket, "getaddrinfo", d)
    monkeypatch.delenv("LUXE_WEB_ALLOW_PRIVATE", raising=False)
    monkeypatch.delenv("LUXE_WEB_ALLOWLIST", raising=False)
    return d


@pytest.fixture()
def loopback_is_public(monkeypatch):
    real = fetch_mod._is_public_ip
    monkeypatch.setattr(fetch_mod, "_is_public_ip",
                        lambda ip: ip == "127.0.0.1" or real(ip))


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_GET(self):  # noqa: N802
        _Handler.seen.append({"path": self.path, **dict(self.headers)})
        if self.path == "/out":
            self.send_response(302)
            self.send_header("Location", "http://internal.test/secret")
            self.end_headers()
            return
        if self.path == "/bomb":
            body = gzip.compress(b"\0" * 5_000_000)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Encoding", "gzip")
        else:
            body = b"PUBLIC-PAGE"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _FastServer(http.server.HTTPServer):
    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


@pytest.fixture()
def port():
    _Handler.seen = []
    srv = _FastServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_port
    srv.shutdown()


# --- DNS rebinding -----------------------------------------------------------

def test_connect_is_pinned_to_the_checked_address(dns, loopback_is_public,
                                                  port):
    """The name answers 127.0.0.1 (checked) then ::1 (the rebind). The
    request must connect to the CHECKED address and never resolve again."""
    dns.answers["rebind.test"] = ["127.0.0.1", "::1"]
    r = fetch_url(f"http://rebind.test:{port}/page")
    assert r.text == "PUBLIC-PAGE"
    assert dns.calls["rebind.test"] == 1
    # The name is kept for the Host header — only the socket is pinned.
    assert _Handler.seen[-1]["Host"] == f"rebind.test:{port}"


def test_redirect_hop_to_a_private_name_is_refused(dns, loopback_is_public,
                                                   port):
    dns.answers["pub.test"] = ["127.0.0.1"]
    dns.answers["internal.test"] = ["10.0.0.7"]
    with pytest.raises(WebError, match="10.0.0.7"):
        fetch_url(f"http://pub.test:{port}/out")


def test_environment_proxies_are_ignored(dns, loopback_is_public, port,
                                         monkeypatch):
    """A proxy resolves the name itself, which would undo the pin."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    dns.answers["pub.test"] = ["127.0.0.1"]
    assert fetch_url(f"http://pub.test:{port}/x").text == "PUBLIC-PAGE"


# --- parser differentials ----------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000\\@example.com/",   # WHATWG: host 127.0.0.1
    "http://example.com\\@evil.test/",
    "http://0177.0.0.1/",                     # octal
    "http://0x7f.0.0.1/",                     # hex
    "http://0x7f000001/",
    "http://2130706433/",                     # 32-bit decimal
    "http://127.1/",                          # short form
    "http://127.0.0.1./",
    "http://exa mple.com/",
    "http://example.com/a\tb",
    "http://example.com/\x00",
    "http://user:pw@example.com/",
    "http://[fe80::1%25en0]/",
])
def test_ambiguous_urls_are_refused_before_resolution(dns, url):
    with pytest.raises(WebError):
        resolve_public(url)
    assert dns.calls == {}


def test_fullwidth_digits_normalize_then_hit_the_ip_check(dns):
    with pytest.raises(WebError):
        resolve_public("http://１２７.０.０.１/")


def test_resolve_returns_the_reserialized_url_and_pin(dns):
    dns.answers["docs.example"] = ["93.184.216.34"]
    t = resolve_public("HTTPS://Docs.Example/a b".replace(" ", "%20"))
    assert t.host == "docs.example" and t.ip == "93.184.216.34"
    assert t.url == "https://docs.example/a%20b" and t.port == 443


def test_any_private_answer_among_several_is_refused(dns, monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: (_gai_answer("93.184.216.34", 80)
                         + _gai_answer("100.64.1.2", 80)))
    with pytest.raises(WebError, match="100.64.1.2"):
        resolve_public("http://mixed.test/")


def test_name_resolution_is_bounded_in_time(dns, monkeypatch):
    def _slow(*a, **k):
        time.sleep(3)
        return _gai_answer("93.184.216.34", 80)

    monkeypatch.setattr(socket, "getaddrinfo", _slow)
    t0 = time.monotonic()
    with pytest.raises(WebError, match="resolving"):
        resolve_public("http://slow.test/", dns_timeout_s=0.2)
    assert time.monotonic() - t0 < 2


# --- decompression bomb ------------------------------------------------------

def test_identity_encoding_is_requested(dns, loopback_is_public, port):
    dns.answers["pub.test"] = ["127.0.0.1"]
    fetch_url(f"http://pub.test:{port}/x")
    assert _Handler.seen[-1]["Accept-Encoding"] == "identity"


def test_forced_gzip_is_capped_on_decoded_bytes(dns, loopback_is_public, port):
    dns.answers["pub.test"] = ["127.0.0.1"]
    r = fetch_url(f"http://pub.test:{port}/bomb", max_bytes=1000)
    assert r.truncated and len(r.text) == 1000


def test_bounded_decoder_never_inflates_past_the_room():
    bomb = gzip.compress(b"\0" * 5_000_000)
    dec = fetch_mod._BoundedDecoder("gzip")
    assert len(dec.feed(bomb, 1000)) == 1000
