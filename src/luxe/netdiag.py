"""Layered network diagnostics — bounded, offline-safe, classifier included.

Born from chat session 5bb630813c21 (2026-07-31, in-flight Wi-Fi): the model
re-invented a DNS→TCP→TLS→HTTP probe ladder with hand-rolled curl commands,
hung 9m40s on one with no --max-time, and needed user-prompted discipline to
stay bounded. This module is the purpose-built instrument: every probe has a
hard deadline, runs in-process (no subprocess to hang), and the ladder ends
in a VERDICT that names the failure layer instead of describing symptoms.

Three surfaces share it:
  - `net_probe` tool (chat-only, read-only, via the extra-tool seam) — the
    model runs ONE bounded call instead of eight curls;
  - `/net` in chat + `luxe net` CLI — deterministic report, no model in the
    loop, includes the configured `backends:` endpoints (fallback-kit: which
    of my resources survive this network?);
  - `luxe net --watch N` — re-probe on an interval, print verdict TRANSITIONS
    (the "when did HTTPS come back" question).

Design rules:
  - No probe may block past its deadline: DNS runs via a daemon-thread future
    (getaddrinfo has no timeout parameter); sockets/httpx carry explicit
    timeouts. Total ladder wall ≈ the slowest single layer, not the sum —
    layers that need each other run sequentially, independent ones in threads.
  - /doctor's offline-purity contract is untouched — network analysis lives
    HERE, never as more doctor lines (chat.sdd: the `update` check is the one
    networked doctor line).
  - Pure logic + dataclasses in this module; rich rendering stays in the
    surfaces (commands.py / cli.py). Tests drive the probes against local
    synthetic servers (tests/test_netdiag.py).
"""

from __future__ import annotations

import concurrent.futures
import socket
import ssl
import time
from dataclasses import dataclass, field

# Default per-layer deadlines (seconds). Deliberately short: this is a
# diagnostic, and "slow" is itself a finding (see _SLOW_MS).
DNS_TIMEOUT_S = 3.0
TCP_TIMEOUT_S = 4.0
TLS_TIMEOUT_S = 5.0
HTTP_TIMEOUT_S = 6.0

# An HTTPS round-trip slower than this is reported as degraded even though it
# succeeded (session 5bb630813c21 saw 3-11s per request on plane Wi-Fi).
_SLOW_MS = 3000.0

# Public anchor for the full ladder. apple.com terminates TLS everywhere and
# is a poor censorship target; the portal probe uses Apple's own convention.
ANCHOR_HOST = "apple.com"
# Captive-portal detection endpoint (macOS convention): plain HTTP, expected
# body contains "Success". A redirect or any other body = something between
# us and the internet is answering for it.
PORTAL_URL = "http://captive.apple.com/hotspot-detect.html"
# DNS-free rung: reaching this IP separates "resolver broken" from "offline".
DNSLESS_IP = "1.1.1.1"

# Verdicts, most-broken first. classify() returns the first that matches.
V_OFFLINE = "offline"
V_DNS_BROKEN = "dns-broken"
V_TCP_BLOCKED = "tcp-blocked"
V_PORTAL = "captive-portal"
V_TLS_INTERCEPTED = "tls-intercepted"
V_TLS_BLOCKED = "tls-blocked"
V_DEGRADED = "degraded"
V_OK = "ok"

_ADVICE = {
    V_OFFLINE: "no route to the internet — check the link (Wi-Fi joined? cable?)",
    V_DNS_BROKEN: "raw connectivity works but name resolution fails — try a "
                  "different resolver (networksetup -setdnsservers Wi-Fi 1.1.1.1)",
    V_TCP_BLOCKED: "DNS resolves but TCP connects fail — port-level filtering",
    V_PORTAL: "a captive portal is answering for the internet — open a browser "
              "and authenticate (http://captive.apple.com)",
    V_TLS_INTERCEPTED: "TLS completes but the certificate is untrusted — the "
                       "network is intercepting HTTPS (portal/MITM); do not "
                       "send credentials",
    V_TLS_BLOCKED: "TCP works but TLS handshakes never complete — deep-packet "
                   "filtering (in-flight Wi-Fi signature); plain HTTP may work",
    V_DEGRADED: "everything works but slowly — expect multi-second requests "
                "and stretched timeouts",
    V_OK: "all layers healthy",
}


@dataclass
class Probe:
    layer: str          # dns | tcp | tls | http | https | portal | endpoint
    target: str
    ok: bool
    ms: float
    detail: str = ""    # human-readable: IPs, status, TLS version, issuer …
    error: str = ""     # "" when ok


@dataclass
class LadderReport:
    host: str
    probes: list[Probe] = field(default_factory=list)
    verdict: str = V_OK
    advice: str = ""


def _timed(fn):
    """Run fn(), return (value, ms). fn returns (ok, detail, error)."""
    t0 = time.monotonic()
    ok, detail, error = fn()
    return ok, detail, error, (time.monotonic() - t0) * 1000.0


# --- individual probes -------------------------------------------------------


def probe_dns(host: str, timeout: float = DNS_TIMEOUT_S) -> Probe:
    """Resolve `host`. getaddrinfo has NO timeout parameter and can block on a
    dead resolver, so it runs on a daemon thread we abandon on deadline."""
    def _resolve():
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = []
        for _f, _t, _p, _c, sockaddr in infos:
            ip = sockaddr[0]
            if ip not in ips:
                ips.append(ip)
        return ips

    t0 = time.monotonic()
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="netdiag-dns")
    try:
        fut = pool.submit(_resolve)
        try:
            ips = fut.result(timeout=timeout)
            ms = (time.monotonic() - t0) * 1000.0
            return Probe("dns", host, True, ms, detail=", ".join(ips[:3]))
        except concurrent.futures.TimeoutError:
            return Probe("dns", host, False, timeout * 1000.0,
                         error=f"no answer in {timeout:.0f}s (resolver dead?)")
        except socket.gaierror as e:
            ms = (time.monotonic() - t0) * 1000.0
            return Probe("dns", host, False, ms, error=f"resolution failed: {e}")
    finally:
        pool.shutdown(wait=False)


def probe_tcp(host: str, port: int = 443,
              timeout: float = TCP_TIMEOUT_S) -> Probe:
    target = f"{host}:{port}"

    def _connect():
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, "connected", ""
        except socket.timeout:
            return False, "", f"connect timed out after {timeout:.0f}s"
        except OSError as e:
            return False, "", f"{type(e).__name__}: {e}"

    ok, detail, error, ms = _timed(_connect)
    return Probe("tcp", target, ok, ms, detail=detail, error=error)


def probe_tls(host: str, port: int = 443,
              timeout: float = TLS_TIMEOUT_S) -> Probe:
    """TLS handshake with SNI + certificate verification. The three failure
    shapes carry distinct meanings (see classify): a handshake TIMEOUT is
    filtering; an UNTRUSTED certificate is interception; a clean handshake
    reports version + issuer."""
    target = f"{host}:{port}"

    def _shake():
        ctx = ssl.create_default_context()
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw:
                raw.settimeout(timeout)
                with ctx.wrap_socket(raw, server_hostname=host) as tls:
                    cert = tls.getpeercert() or {}
                    issuer = "?"
                    for rdn in cert.get("issuer", ()):
                        for key, val in rdn:
                            if key in ("organizationName", "commonName"):
                                issuer = val
                    return True, f"{tls.version()} · issuer {issuer}", ""
        except ssl.SSLCertVerificationError as e:
            return False, "", f"handshake completed but cert UNTRUSTED: {e.verify_message}"
        except (socket.timeout, TimeoutError):
            return False, "", f"handshake timed out after {timeout:.0f}s"
        except (ssl.SSLError, OSError) as e:
            return False, "", f"{type(e).__name__}: {e}"

    ok, detail, error, ms = _timed(_shake)
    return Probe("tls", target, ok, ms, detail=detail, error=error)


def probe_http(url: str, timeout: float = HTTP_TIMEOUT_S,
               layer: str | None = None) -> Probe:
    """Plain GET; follows no redirects (a portal's 302 is signal, not noise)."""
    import httpx

    lay = layer or ("https" if url.startswith("https") else "http")

    def _get():
        try:
            r = httpx.get(url, timeout=timeout, follow_redirects=False)
            loc = r.headers.get("location", "")
            det = f"HTTP {r.status_code}" + (f" → {loc[:60]}" if loc else "")
            return True, det, ""
        except httpx.TimeoutException:
            return False, "", f"no response in {timeout:.0f}s"
        except httpx.HTTPError as e:
            return False, "", f"{type(e).__name__}: {e}"

    ok, detail, error, ms = _timed(_get)
    return Probe(lay, url, ok, ms, detail=detail, error=error)


def probe_portal(timeout: float = HTTP_TIMEOUT_S) -> Probe:
    """Captive-portal check: expected = 200 with 'Success' in the body.
    ok=True means NO portal. A redirect or foreign body flips ok=False with
    the evidence in `error`."""
    import httpx

    def _get():
        try:
            r = httpx.get(PORTAL_URL, timeout=timeout, follow_redirects=False)
            if r.status_code == 200 and "Success" in r.text:
                return True, "no portal", ""
            loc = r.headers.get("location", "")
            return False, "", (f"HTTP {r.status_code}"
                               + (f" → {loc[:80]}" if loc else "")
                               + " — a portal is answering for the internet")
        except httpx.HTTPError as e:
            # Unreachable ≠ portal; report as inconclusive failure.
            return False, "", f"unreachable ({type(e).__name__})"

    ok, detail, error, ms = _timed(_get)
    return Probe("portal", PORTAL_URL, ok, ms, detail=detail, error=error)


def probe_endpoint(name: str, base_url: str,
                   timeout: float = TCP_TIMEOUT_S) -> Probe:
    """One configured oMLX endpoint: GET /health (200 = up)."""
    import httpx

    from luxe.backend import is_loopback_url

    url = base_url.rstrip("/") + "/health"
    # A local endpoint is probed WITHOUT any proxy, so the doctor reports the
    # endpoint's own health rather than a stale system proxy's (`is_loopback_url`).
    # The rungs above deliberately keep `trust_env` — they are measuring the
    # network the environment describes.
    trust_env = not is_loopback_url(base_url)

    def _get():
        try:
            r = httpx.get(url, timeout=timeout, trust_env=trust_env)
            if r.status_code == 200:
                return True, "healthy", ""
            return False, "", f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            return False, "", f"{type(e).__name__}: {e}"

    ok, detail, error, ms = _timed(_get)
    return Probe("endpoint", f"{name} ({base_url})", ok, ms,
                 detail=detail, error=error)


# --- ladder + classification -------------------------------------------------


def run_ladder(host: str = ANCHOR_HOST) -> LadderReport:
    """The full layered probe for one host + the DNS-free and portal rungs.
    Sequential where a layer informs the next, threaded where independent;
    worst-case wall ≈ one slow layer, not the sum of all deadlines."""
    report = LadderReport(host=host)
    dns = probe_dns(host)
    report.probes.append(dns)

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=6, thread_name_prefix="netdiag") as pool:
        futs = {
            "tcp": pool.submit(probe_tcp, host, 443),
            "tls": pool.submit(probe_tls, host, 443),
            "http": pool.submit(probe_http, f"http://{host}/"),
            "https": pool.submit(probe_http, f"https://{host}/"),
            "portal": pool.submit(probe_portal),
            "dnsless": pool.submit(probe_tcp, DNSLESS_IP, 443),
        }
        results = {k: f.result() for k, f in futs.items()}

    results["dnsless"].detail = (results["dnsless"].detail
                                 or "") + " (no DNS involved)"
    report.probes += [results[k] for k in
                      ("tcp", "tls", "http", "https", "portal", "dnsless")]
    report.verdict = classify(report.probes)
    report.advice = _ADVICE[report.verdict]
    return report


def _get(probes: list[Probe], layer: str) -> Probe | None:
    return next((p for p in probes if p.layer == layer), None)


def classify(probes: list[Probe]) -> str:
    """Decision tree over the ladder, most-broken verdict first. Pure — unit
    tested against synthetic probe lists."""
    dns = _get(probes, "dns")
    tcp = _get(probes, "tcp")
    tls = _get(probes, "tls")
    https = _get(probes, "https")
    portal = _get(probes, "portal")
    dnsless = next((p for p in probes if p.layer == "tcp"
                    and p.target.startswith(DNSLESS_IP)), None)

    dns_ok = dns is None or dns.ok
    tcp_ok = (tcp is not None and tcp.ok) or (dnsless is not None and dnsless.ok)

    if not dns_ok and not tcp_ok:
        return V_OFFLINE
    if not dns_ok:
        return V_DNS_BROKEN
    if tcp is not None and not tcp.ok and (dnsless is None or not dnsless.ok):
        return V_OFFLINE
    if tcp is not None and not tcp.ok:
        return V_TCP_BLOCKED
    if portal is not None and not portal.ok and "portal" in portal.error:
        return V_PORTAL
    if tls is not None and not tls.ok:
        if "UNTRUSTED" in tls.error:
            return V_TLS_INTERCEPTED
        return V_TLS_BLOCKED
    if https is not None and https.ok and https.ms > _SLOW_MS:
        return V_DEGRADED
    if https is not None and not https.ok:
        return V_TLS_BLOCKED
    return V_OK


# --- full report (ladder + configured fallback resources) --------------------


@dataclass
class NetReport:
    ladder: LadderReport
    endpoints: list[Probe] = field(default_factory=list)


def full_report(cfg=None, host: str = ANCHOR_HOST) -> NetReport:
    """The `/net` / `luxe net` payload: the public ladder plus every
    configured `backends:` endpoint (fallback-kit: which of my resources
    survive this network?). `cfg` is a PipelineConfig or None (ladder only).
    Endpoint probes run concurrently with the ladder's own pool teardown."""
    entries = {}
    if cfg is not None:
        try:
            entries = cfg.backend_entries()
        except Exception:
            entries = {}

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="netdiag-ep") as pool:
        lad_fut = pool.submit(run_ladder, host)
        ep_futs = [pool.submit(probe_endpoint, name, e.base_url)
                   for name, e in entries.items()]
        ladder = lad_fut.result()
        endpoints = [f.result() for f in ep_futs]
    return NetReport(ladder=ladder, endpoints=endpoints)


def render_lines(report: NetReport) -> list[tuple[bool, str]]:
    """(ok, text) pairs for the surfaces to glyph/colour. Pure text here —
    rich markup belongs to the renderer (commands.py / cli.py)."""
    lines: list[tuple[bool, str]] = []
    for p in report.ladder.probes:
        body = p.detail if p.ok else p.error
        lines.append((p.ok, f"{p.layer:<8} {p.target:<40} {p.ms:6.0f}ms  {body}"))
    for p in report.endpoints:
        body = p.detail if p.ok else p.error
        lines.append((p.ok, f"{p.layer:<8} {p.target:<40} {p.ms:6.0f}ms  {body}"))
    return lines


# --- chat tool ---------------------------------------------------------------

_NET_PROBE_DESC = (
    "Layered network connectivity probe: DNS resolution, TCP connect, TLS "
    "handshake (with certificate verification), plain HTTP, HTTPS, and a "
    "captive-portal check — every layer hard-bounded to a few seconds, "
    "finishing with a verdict naming the broken layer. USE THIS instead of "
    "curl/ping/nc for any connectivity question: it cannot hang and one call "
    "covers the whole ladder. Optionally pass a host to probe a specific "
    "service (e.g. api.anthropic.com); default probes a public anchor."
)

_NET_PROBE_PARAMS = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "description": "Hostname to probe (default: a public anchor host)",
        },
    },
    "required": [],
}


def make_net_probe_tool():
    """(ToolDef, ToolFn) pair for the chat extra-tool seam (same pattern as
    update_ledger). Read-only, in-process, bounded — safe in every session
    mode including no-project and read-only."""
    from luxe.tools.base import ToolDef

    def _fn(args: dict) -> tuple[str, str | None]:
        try:
            host = str((args or {}).get("host") or ANCHOR_HOST).strip()
            report = run_ladder(host)
            lines = [f"verdict: {report.verdict} — {report.advice}"]
            for p in report.probes:
                mark = "ok " if p.ok else "FAIL"
                body = p.detail if p.ok else p.error
                lines.append(f"[{mark}] {p.layer:<7} {p.target} "
                             f"({p.ms:.0f}ms) {body}")
            return "\n".join(lines), None
        except Exception as e:  # a diagnostic must never crash the loop
            return "", f"{type(e).__name__}: {e}"

    defn = ToolDef(name="net_probe", description=_NET_PROBE_DESC,
                   parameters=_NET_PROBE_PARAMS)
    return defn, _fn
