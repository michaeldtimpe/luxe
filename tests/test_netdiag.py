"""netdiag — layered network probes, classifier, and the chat tool.

Everything runs against LOCAL synthetic servers (silent TCP for handshake
timeouts, http.server for HTTP/portal shapes, a self-signed TLS server for
the interception case) or monkeypatched resolvers — no test touches the real
network, and every probe must return within its declared deadline.
"""

from __future__ import annotations

import http.server
import socket
import socketserver
import ssl
import threading
import time

import pytest

from luxe import netdiag
from luxe.netdiag import (
    LadderReport,
    NetReport,
    Probe,
    classify,
    make_net_probe_tool,
    probe_dns,
    probe_endpoint,
    probe_http,
    probe_portal,
    probe_tcp,
    probe_tls,
)


# --- local server helpers ----------------------------------------------------


@pytest.fixture
def silent_server():
    """Accepts TCP connections and never sends a byte — the TLS-filtering
    signature (handshake sent, no reply)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    stop = threading.Event()
    conns = []

    def _loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                conns.append(c)  # hold open, never respond
            except socket.timeout:
                continue
            except OSError:
                break

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    yield port
    stop.set()
    for c in conns:
        c.close()
    srv.close()


class _Handler(http.server.BaseHTTPRequestHandler):
    """Path-keyed responses: /success (portal-free body), /portal (302),
    /health (200), anything else 200 'hello'."""

    def do_GET(self):  # noqa: N802 (stdlib naming)
        if self.path.startswith("/portal"):
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.1/login")
            self.end_headers()
            return
        body = b"Success" if self.path.startswith("/success") else b"hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # keep pytest output clean
        pass


@pytest.fixture
def http_server():
    with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as httpd:
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        yield port
        httpd.shutdown()


def _self_signed_cert(tmp_path):
    """Generate a self-signed localhost cert (cryptography is in the venv)."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    certfile = tmp_path / "cert.pem"
    keyfile = tmp_path / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return str(certfile), str(keyfile)


@pytest.fixture
def selfsigned_tls_server(tmp_path):
    """A real TLS server whose cert no client trusts — the interception
    (captive portal / MITM) signature: handshake completes, verification
    fails."""
    certfile, keyfile = _self_signed_cert(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def _loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                raw, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                tls = ctx.wrap_socket(raw, server_side=True)
                tls.close()
            except (ssl.SSLError, OSError):
                raw.close()

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    yield port
    stop.set()
    srv.close()


# --- individual probes -------------------------------------------------------


def test_tcp_probe_connects(http_server):
    p = probe_tcp("127.0.0.1", http_server, timeout=2.0)
    assert p.ok and p.layer == "tcp"


def test_tcp_probe_fails_fast_on_closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listening now
    t0 = time.monotonic()
    p = probe_tcp("127.0.0.1", port, timeout=2.0)
    assert not p.ok
    assert time.monotonic() - t0 < 3.0


def test_tls_probe_times_out_bounded_on_silent_server(silent_server):
    """The plane signature: TCP accepts, handshake never answered. The probe
    must return within its deadline — never hang like the curls did."""
    t0 = time.monotonic()
    p = probe_tls("127.0.0.1", silent_server, timeout=1.0)
    wall = time.monotonic() - t0
    assert not p.ok
    assert "timed out" in p.error
    assert wall < 4.0  # deadline + slack, nowhere near a hang


def test_tls_probe_flags_untrusted_cert(selfsigned_tls_server):
    p = probe_tls("127.0.0.1", selfsigned_tls_server, timeout=3.0)
    assert not p.ok
    assert "UNTRUSTED" in p.error


def test_http_probe_ok(http_server):
    p = probe_http(f"http://127.0.0.1:{http_server}/x", timeout=3.0)
    assert p.ok and "200" in p.detail and p.layer == "http"


def test_portal_probe_clean_network(http_server, monkeypatch):
    monkeypatch.setattr(netdiag, "PORTAL_URL",
                        f"http://127.0.0.1:{http_server}/success")
    p = probe_portal(timeout=3.0)
    assert p.ok


def test_portal_probe_detects_redirect(http_server, monkeypatch):
    monkeypatch.setattr(netdiag, "PORTAL_URL",
                        f"http://127.0.0.1:{http_server}/portal")
    p = probe_portal(timeout=3.0)
    assert not p.ok
    assert "portal" in p.error and "10.0.0.1" in p.error


def test_endpoint_probe_health(http_server):
    p = probe_endpoint("local", f"http://127.0.0.1:{http_server}", timeout=3.0)
    assert p.ok and p.detail == "healthy"


def test_dns_probe_resolves(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    p = probe_dns("example.com", timeout=2.0)
    assert p.ok and "93.184.216.34" in p.detail


def test_dns_probe_reports_nxdomain(monkeypatch):
    def _raise(*a, **k):
        raise socket.gaierror(8, "nodename nor servname provided")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    p = probe_dns("no-such-host.invalid", timeout=2.0)
    assert not p.ok and "resolution failed" in p.error


def test_dns_probe_bounded_when_resolver_hangs(monkeypatch):
    """getaddrinfo has no timeout parameter — a dead resolver must not hang
    the probe (the module runs it on an abandoned daemon thread)."""
    def _hang(*a, **k):
        time.sleep(5)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", _hang)
    t0 = time.monotonic()
    p = probe_dns("example.com", timeout=0.5)
    assert not p.ok and "no answer" in p.error
    assert time.monotonic() - t0 < 2.0


# --- classifier (pure) -------------------------------------------------------


def _P(layer, ok, target="t", ms=10.0, error=""):
    return Probe(layer, target, ok, ms, error=error)


def test_classify_ok():
    probes = [_P("dns", True), _P("tcp", True), _P("tls", True),
              _P("https", True), _P("portal", True)]
    assert classify(probes) == netdiag.V_OK


def test_classify_offline_everything_dead():
    probes = [_P("dns", False), _P("tcp", False),
              _P("tcp", False, target=f"{netdiag.DNSLESS_IP}:443")]
    assert classify(probes) == netdiag.V_OFFLINE


def test_classify_dns_broken_when_raw_ip_works():
    probes = [_P("dns", False), _P("tcp", False),
              _P("tcp", True, target=f"{netdiag.DNSLESS_IP}:443")]
    assert classify(probes) == netdiag.V_DNS_BROKEN


def test_classify_tcp_blocked():
    probes = [_P("dns", True), _P("tcp", False),
              _P("tcp", True, target=f"{netdiag.DNSLESS_IP}:443")]
    assert classify(probes) == netdiag.V_TCP_BLOCKED


def test_classify_captive_portal_wins_over_tls():
    probes = [_P("dns", True), _P("tcp", True),
              _P("tls", False, error="handshake timed out after 5s"),
              _P("portal", False, error="HTTP 302 → http://x — a portal is "
                                        "answering for the internet")]
    assert classify(probes) == netdiag.V_PORTAL


def test_classify_tls_blocked_the_plane_case():
    """Session 5bb630813c21: DNS ✓, TCP ✓, handshake never completes."""
    probes = [_P("dns", True), _P("tcp", True),
              _P("tls", False, error="handshake timed out after 5s"),
              _P("portal", True)]
    assert classify(probes) == netdiag.V_TLS_BLOCKED


def test_classify_tls_intercepted():
    probes = [_P("dns", True), _P("tcp", True),
              _P("tls", False, error="handshake completed but cert UNTRUSTED: x"),
              _P("portal", True)]
    assert classify(probes) == netdiag.V_TLS_INTERCEPTED


def test_classify_degraded_when_https_slow():
    probes = [_P("dns", True), _P("tcp", True), _P("tls", True),
              _P("https", True, ms=8000.0), _P("portal", True)]
    assert classify(probes) == netdiag.V_DEGRADED


# --- ladder assembly + report ------------------------------------------------


def _fake_ladder_probes(monkeypatch):
    monkeypatch.setattr(netdiag, "probe_dns",
                        lambda host, timeout=0: _P("dns", True, host))
    monkeypatch.setattr(netdiag, "probe_tcp",
                        lambda host, port=443, timeout=0:
                        _P("tcp", True, f"{host}:{port}"))
    monkeypatch.setattr(netdiag, "probe_tls",
                        lambda host, port=443, timeout=0:
                        _P("tls", True, f"{host}:{port}"))
    monkeypatch.setattr(netdiag, "probe_http",
                        lambda url, timeout=0, layer=None:
                        _P(layer or ("https" if url.startswith("https")
                                     else "http"), True, url))
    monkeypatch.setattr(netdiag, "probe_portal",
                        lambda timeout=0: _P("portal", True))


def test_run_ladder_assembles_all_layers(monkeypatch):
    _fake_ladder_probes(monkeypatch)
    report = netdiag.run_ladder("example.com")
    layers = [p.layer for p in report.probes]
    assert layers == ["dns", "tcp", "tls", "http", "https", "portal", "tcp"]
    assert report.verdict == netdiag.V_OK
    assert report.advice


def test_full_report_probes_configured_backends(monkeypatch, http_server):
    from luxe.config import BackendEntry, PipelineConfig, RoleConfig

    _fake_ladder_probes(monkeypatch)
    cfg = PipelineConfig(
        models={"monolith": "M"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        backends={"local": BackendEntry(
            base_url=f"http://127.0.0.1:{http_server}", default=True)},
    )
    report = netdiag.full_report(cfg, host="example.com")
    assert len(report.endpoints) == 1
    assert report.endpoints[0].ok
    assert "local" in report.endpoints[0].target


def test_full_report_without_config_is_ladder_only(monkeypatch):
    _fake_ladder_probes(monkeypatch)
    report = netdiag.full_report(None, host="example.com")
    assert report.endpoints == []
    assert report.ladder.verdict == netdiag.V_OK


# --- the chat tool -----------------------------------------------------------


def test_net_probe_tool_returns_verdict_first(monkeypatch):
    canned = LadderReport(host="h", probes=[_P("dns", True)],
                          verdict=netdiag.V_TLS_BLOCKED,
                          advice=netdiag._ADVICE[netdiag.V_TLS_BLOCKED])
    monkeypatch.setattr(netdiag, "run_ladder", lambda host: canned)
    defn, fn = make_net_probe_tool()
    assert defn.name == "net_probe"
    out, err = fn({"host": "h"})
    assert err is None
    assert out.splitlines()[0].startswith("verdict: tls-blocked")


def test_net_probe_tool_never_raises(monkeypatch):
    def _boom(host):
        raise RuntimeError("wires cut")

    monkeypatch.setattr(netdiag, "run_ladder", _boom)
    _defn, fn = make_net_probe_tool()
    out, err = fn({})
    assert out == "" and "wires cut" in err


def test_render_lines_marks_failures():
    report = NetReport(
        ladder=LadderReport(host="h", probes=[
            _P("dns", True), _P("tls", False, error="handshake timed out")]),
        endpoints=[_P("endpoint", False, target="m5", error="unreachable")])
    lines = netdiag.render_lines(report)
    assert [ok for ok, _ in lines] == [True, False, False]
    assert "handshake timed out" in lines[1][1]
