"""Bounded HTTP fetch with an egress guard.

Every limit here exists because the failure it prevents is worse than the
capability it costs:

- **Egress guard** (`resolve_public`). luxe runs on a tailnet next to
  privileged mage-hands relays, an oMLX endpoint on localhost, and a NAS.
  A model that can fetch arbitrary URLs can otherwise be talked into
  `http://localhost:8000`, `https://kappa.tailca7308.ts.net/mcp`, or cloud
  metadata at 169.254.169.254 — SSRF against the operator's own fleet. The
  guard resolves the hostname ONCE, refuses any non-public address, and
  returns the checked IP; the request then CONNECTS TO THAT IP (Host header
  and TLS SNI/verification still use the name). Checking one resolution and
  letting the HTTP stack do a second is a DNS-rebinding hole: a name with a
  zero TTL answers "public" to the check and 127.0.0.1 to the connect. It
  re-runs on EVERY redirect hop (a public host can 302 to 127.0.0.1).
- **One parser**. A URL is only checked in the form it will be used. Hosts
  that different URL parsers read differently (a `\\` before `@`, octal/hex/
  short-form IPv4 like `0177.0.0.1` or `2130706433`, whitespace, control
  characters) are refused outright rather than guessed at — urlsplit,
  getaddrinfo and a WHATWG browser each disagree about some of them.
- **Size cap**. Read raw bytes in chunks and stop; `Accept-Encoding:
  identity` is requested, and a server that compresses anyway is decoded
  incrementally with a ceiling — the cap bounds DECODED bytes, so a 10 KB
  gzip bomb cannot become a multi-GB string in a chat turn.
- **Time cap**. `httpx` timeouts are per-read, so a trickling response can
  outlive any of them; a total deadline bounds wall time as well, and name
  resolution itself is bounded (a wedged resolver must not hang a turn).

`LUXE_WEB_ALLOW_PRIVATE=1` lifts the egress guard for local development
(e.g. scraping a dev server on localhost). It is deliberately an env var and
not a tool argument — the model must not be able to talk itself past it.

Proxies from the environment are NOT honoured (`trust_env=False`): a proxy
resolves the name itself, which would silently undo the pin.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
import time
import zlib
from dataclasses import dataclass, field

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_BYTES = 2_000_000        # 2 MB of source before extraction
DEFAULT_MAX_REDIRECTS = 5
# Name resolution gets its own, shorter bound: getaddrinfo has no timeout
# argument and a broken resolver can block for the platform default (30s+).
DNS_TIMEOUT_S = 8.0
USER_AGENT = "luxe/1.0 (+https://github.com/michaeldtimpe/luxe)"

_ALLOWED_SCHEMES = ("http", "https")
# Before the query: `\` is a path separator to a WHATWG browser but an
# ordinary character to urlsplit, so `http://127.0.0.1:8000\@example.com/`
# is example.com to one and 127.0.0.1 to the other.
_BAD_ANYWHERE = re.compile(r"[\x00-\x20\x7f]")
# A canonical ASCII host: LDH labels (underscores tolerated; real hosts use
# them), or an IPv6 literal checked separately.
_HOSTNAME_RE = re.compile(r"^[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?"
                          r"(\.[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?)*\.?$")
# The WHATWG IPv4 rule: a host whose LAST label is numeric (decimal or 0x…)
# is parsed as an IPv4 number, octal and short forms included.
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")


class WebError(RuntimeError):
    """Any refusal or failure in the web layer. Carries an operator-readable
    message — these surface directly to the model as tool errors."""


@dataclass
class FetchResult:
    url: str                     # final URL after redirects
    status: int
    content_type: str
    text: str                    # decoded body (possibly truncated)
    truncated: bool = False
    elapsed_s: float = 0.0
    redirects: list[str] = field(default_factory=list)

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type.lower()


@dataclass(frozen=True)
class Target:
    """A URL that passed the guard, in the one form it may be used."""
    url: str                     # re-serialized canonical URL
    scheme: str
    host: str                    # canonical ASCII host (IDNA, lowercase)
    port: int
    ip: str                      # the checked address the request pins to


def _allow_private() -> bool:
    return os.environ.get("LUXE_WEB_ALLOW_PRIVATE", "") == "1"


def _host_allowlist() -> tuple[str, ...]:
    """Optional fnmatch host allowlist from `LUXE_WEB_ALLOWLIST`.

    Unset ⇒ empty ⇒ **no host restriction** (the IP-class guard below still
    applies). Set ⇒ deny-by-default: only matching hosts are reachable.

    Inherited from the 2026-08-02 `browser.py` stack, which was allowlist-only
    and deny-by-default with a fixed 11-domain list. That is the right posture
    for a locked-down deployment and the wrong default for a dev tool — a
    hardcoded list silently refuses the docs page you actually need. Note an
    allowlist alone is NOT a substitute for the IP guard: it says nothing
    about where a name RESOLVES, so it cannot stop the tailnet/CGNAT case.
    The two layers compose; neither replaces the other.
    """
    raw = os.environ.get("LUXE_WEB_ALLOWLIST", "")
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


def _is_public_ip(ip: str) -> bool:
    """True only for addresses reachable on the public internet.

    `is_global` carries the load because the obvious predicate does NOT: the
    tailnet lives in 100.64.0.0/10 (RFC 6598 carrier-grade NAT), which
    `is_private` reports as **False**. Checking only private/loopback/
    link-local would therefore have left every mage-hands relay fetchable —
    exactly the address range this guard exists to protect. The explicit
    categories stay as belt-and-braces in case `is_global` shifts meaning.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if not addr.is_global:
        return False
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_multicast or addr.is_reserved or addr.is_unspecified
    )


def _normalize_url(url: str) -> str:
    """Add https:// when no scheme is present, and reject non-http schemes.

    Order matters: prepending first would turn `data:text/html,x` into
    `https://data:text/html,x`, whose netloc parses as host `data` port
    `text` — urlsplit then raises ValueError on .port instead of our clean
    refusal. Detect the scheme on the ORIGINAL string.
    """
    url = (url or "").strip()
    if not url:
        raise WebError("empty URL")
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", url)
    if m:
        scheme = m.group(1).lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise WebError(
                f"refused scheme `{scheme}` — only http/https are allowed "
                "(no file://, data:, ftp://)")
        return url
    return "https://" + url


def _canonical_host(raw: str, url: str) -> str:
    """Validate the host parsers agree on; return it lowercase ASCII.

    IPv6 literals pass only in canonical-parseable form without a zone id.
    Everything else must be plain LDH after IDNA, and a numeric-looking host
    must be an exact dotted-quad — `0177.0.0.1`, `0x7f.1`, `2130706433` and
    `127.1` are all 127.0.0.1 to a browser and to inet_aton, but not to
    `ipaddress`, so they are refused rather than resolved.
    """
    host = raw.lower()
    if ":" in host:                       # IPv6 literal (brackets stripped)
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise WebError(f"refused host {raw!r} in {url!r} — not a "
                           "canonical IPv6 literal (zone ids are refused)"
                           ) from None
        return host
    if not _HOSTNAME_RE.match(host):
        raise WebError(f"refused host {raw!r} in {url!r} — only plain "
                       "letters, digits, '-', '_' and '.' are accepted")
    last = host.rstrip(".").rsplit(".", 1)[-1]
    if _NUMERIC_LABEL.match(last):
        try:
            canonical = str(ipaddress.IPv4Address(host))
        except ValueError:
            canonical = ""
        if canonical != host:
            raise WebError(
                f"refused host {raw!r} in {url!r} — numeric hosts must be a "
                "plain dotted-quad IPv4 address (octal, hex and short forms "
                "are read differently by different URL parsers)")
    return host


def _getaddrinfo_bounded(host: str, port: int,
                         timeout_s: float) -> list[str]:
    """getaddrinfo on a daemon thread with a wall bound. Returns the IPs."""
    if timeout_s <= 0:
        raise WebError(f"no time left to resolve {host}")
    box: dict = {}

    def _run():
        try:
            box["infos"] = socket.getaddrinfo(host, port,
                                              proto=socket.IPPROTO_TCP)
        except BaseException as e:  # noqa: BLE001 — handed to the caller
            box["err"] = e

    t = threading.Thread(target=_run, name="luxe-web-dns", daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise WebError(f"timed out after {timeout_s:.0f}s resolving {host}")
    if "err" in box:
        raise WebError(f"cannot resolve {host}: {box['err']}")
    ips = [str(info[4][0]) for info in box.get("infos") or []]
    if not ips:
        raise WebError(f"cannot resolve {host}: no addresses")
    return ips


def resolve_public(url: str, *, dns_timeout_s: float = DNS_TIMEOUT_S) -> Target:
    """Parse, validate, resolve ONCE, and check every address.

    The single egress decision for fetch, render, and the page session. The
    returned `Target.url` is the re-serialized form every consumer must use
    (never the model's original string), and `Target.ip` is the address a
    request must connect to.
    """
    import httpx

    url = _normalize_url(url)
    if _BAD_ANYWHERE.search(url):
        raise WebError(f"refused URL {url!r} — whitespace or control "
                       "characters are not allowed; percent-encode them")
    before_query = re.split(r"[?#]", url, maxsplit=1)[0]
    if "\\" in before_query:
        raise WebError(f"refused URL {url!r} — a backslash before the query "
                       "is read differently by browsers and URL parsers")
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError) as e:
        raise WebError(f"unparseable URL {url!r}: {e}") from e
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise WebError(
            f"refused scheme `{parsed.scheme or '(none)'}` — only http/https "
            "are allowed (no file://, data:, ftp://)")
    if parsed.userinfo:
        raise WebError(f"refused URL {url!r} — credentials in the URL "
                       "(user@host) are not supported")
    raw_host = parsed.raw_host.decode("ascii", "replace")
    if not raw_host:
        raise WebError(f"no host in URL: {url!r}")
    host = _canonical_host(raw_host, url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    allowlist = _host_allowlist()
    if allowlist:
        import fnmatch
        if not any(fnmatch.fnmatch(host, pat) for pat in allowlist):
            raise WebError(
                f"refused {host} — not in LUXE_WEB_ALLOWLIST "
                f"({', '.join(allowlist)}). Add a pattern to that env var to "
                "allow it, or unset the variable to allow any public host.")

    try:
        ips = [str(ipaddress.ip_address(host))]   # a literal needs no DNS
    except ValueError:
        ips = _getaddrinfo_bounded(host, port, dns_timeout_s)
    if not _allow_private():
        for ip in ips:
            if not _is_public_ip(ip):
                raise WebError(
                    f"refused {host} — it resolves to the non-public address "
                    f"{ip}. luxe will not fetch private, loopback, or "
                    "link-local hosts from a tool (this protects the local "
                    "oMLX endpoint, the tailnet relays, and cloud metadata). "
                    "Set LUXE_WEB_ALLOW_PRIVATE=1 in the environment if you "
                    "deliberately want to scrape a local address.")
    return Target(url=str(parsed), scheme=parsed.scheme, host=host,
                  port=port, ip=ips[0])


def _assert_public(url: str) -> Target:
    """Back-compat name for the guard; returns the checked `Target`."""
    return resolve_public(url)


# --- pinned transport --------------------------------------------------------

def _pinned_transport(target: Target):
    """An httpx transport whose every TCP connect goes to `target.ip`.

    The URL (and so the Host header, TLS SNI and certificate verification)
    keeps the name; only the socket is pointed at the checked address. Any
    other host is refused at connect time — nothing may escape the pin.
    """
    import httpcore
    import httpx

    class _PinnedBackend(httpcore.SyncBackend):
        def connect_tcp(self, host, port, timeout=None, local_address=None,
                        socket_options=None):
            if host.lower() != target.host or port != target.port:
                raise httpcore.ConnectError(
                    f"refused connect to unpinned {host}:{port}")
            return super().connect_tcp(target.ip, port, timeout=timeout,
                                       local_address=local_address,
                                       socket_options=socket_options)

    transport = httpx.HTTPTransport(trust_env=False)
    # httpx exposes no public network-backend hook; the pool is rebuilt with
    # the same public httpcore constructor httpx itself uses. httpx is held
    # below 1.0 in pyproject so this stays on a known shape.
    transport._pool = httpcore.ConnectionPool(  # noqa: SLF001
        ssl_context=httpx.create_ssl_context(trust_env=False),
        network_backend=_PinnedBackend(),
        max_connections=1,
    )
    return transport


@dataclass
class RawResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    truncated: bool


class _BoundedDecoder:
    """Decode a Content-Encoding incrementally, never past the room left."""

    def __init__(self, encoding: str):
        enc = (encoding or "identity").strip().lower()
        if enc in ("", "identity"):
            self._z = None
        elif enc in ("gzip", "x-gzip"):
            self._z = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif enc == "deflate":
            self._z = zlib.decompressobj()
        else:
            raise WebError(f"server sent unsupported Content-Encoding "
                           f"{encoding!r} despite Accept-Encoding: identity")

    def feed(self, chunk: bytes, room: int) -> bytes:
        if self._z is None:
            return chunk[:room]
        try:
            # max_length leaves the rest in unconsumed_tail; it must be fed
            # back first or the stream loses bytes mid-document.
            data = self._z.unconsumed_tail + chunk
            return self._z.decompress(data, max(room, 1))[:room]
        except zlib.error as e:
            raise WebError(f"corrupt compressed response: {e}") from e


def bounded_request(method: str, url: str, *,
                    timeout_s: float = DEFAULT_TIMEOUT_S,
                    max_bytes: int = DEFAULT_MAX_BYTES,
                    deadline: float | None = None,
                    headers: dict[str, str] | None = None,
                    params: dict | None = None,
                    json: object = None) -> RawResponse:
    """ONE guarded request (no redirect following), pinned and bounded.

    The shared reader for fetch, search and answers (web.sdd: every request
    gets a per-read timeout AND a total deadline, and a byte cap applied
    during streaming). The body is capped AFTER decoding.
    """
    import httpx

    start = time.monotonic()
    deadline = deadline if deadline is not None else start + timeout_s
    remaining = deadline - start
    if remaining <= 0:
        raise WebError(f"timed out fetching {url}")
    target = resolve_public(url, dns_timeout_s=min(DNS_TIMEOUT_S, remaining))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WebError(f"timed out after {timeout_s:.0f}s fetching {url}")

    req_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    req_headers.update(headers or {})
    try:
        with httpx.Client(transport=_pinned_transport(target),
                          follow_redirects=False, trust_env=False,
                          timeout=remaining) as client:
            with client.stream(method, target.url, headers=req_headers,
                               params=params, json=json,
                               timeout=remaining) as resp:
                decoder = _BoundedDecoder(
                    resp.headers.get("content-encoding", ""))
                chunks: list[bytes] = []
                total = 0
                truncated = False
                if not resp.is_redirect:
                    for chunk in resp.iter_raw():
                        if time.monotonic() > deadline:
                            truncated = True
                            break
                        out = decoder.feed(chunk, max_bytes - total)
                        chunks.append(out)
                        total += len(out)
                        if total >= max_bytes:
                            truncated = True
                            break
                return RawResponse(url=str(resp.url), status=resp.status_code,
                                   headers=dict(resp.headers),
                                   body=b"".join(chunks), truncated=truncated)
    except httpx.HTTPError as e:
        raise WebError(f"{type(e).__name__} fetching {target.url}: {e}") from e


def _charset(content_type: str) -> str:
    m = re.search(r"charset=\"?([\w.:-]+)", content_type or "", re.I)
    return m.group(1) if m else "utf-8"


def fetch_url(url: str, *, timeout_s: float = DEFAULT_TIMEOUT_S,
              max_bytes: int = DEFAULT_MAX_BYTES,
              max_redirects: int = DEFAULT_MAX_REDIRECTS,
              headers: dict[str, str] | None = None) -> FetchResult:
    """GET `url` with the egress guard applied to every hop."""
    import httpx

    started = time.monotonic()
    deadline = started + timeout_s
    seen: list[str] = []
    current = _normalize_url(url)

    req_headers = {"Accept": "text/html,application/xhtml+xml,"
                             "application/json;q=0.9,*/*;q=0.8"}
    req_headers.update(headers or {})

    # Redirects are followed BY HAND so the guard re-runs (and re-pins) per
    # hop; httpx's follow_redirects would check only the URL we handed it.
    for _hop in range(max_redirects + 1):
        if deadline - time.monotonic() <= 0:
            raise WebError(f"timed out after {timeout_s:.0f}s fetching {url}")
        resp = bounded_request("GET", current, timeout_s=timeout_s,
                               max_bytes=max_bytes, deadline=deadline,
                               headers=req_headers)
        if 300 <= resp.status < 400 and resp.status != 304:
            location = resp.headers.get("location", "")
            if not location:
                raise WebError(
                    f"{resp.status} redirect with no Location header")
            seen.append(resp.url)
            current = str(httpx.URL(resp.url).join(location))
            continue
        content_type = resp.headers.get("content-type", "")
        try:
            text = resp.body.decode(_charset(content_type), errors="replace")
        except LookupError:
            text = resp.body.decode("utf-8", errors="replace")
        return FetchResult(
            url=resp.url,
            status=resp.status,
            content_type=content_type,
            text=text,
            truncated=resp.truncated,
            elapsed_s=time.monotonic() - started,
            redirects=seen,
        )
    raise WebError(f"too many redirects (>{max_redirects}) starting at {url}")
