"""Bounded HTTP fetch with an egress guard.

Every limit here exists because the failure it prevents is worse than the
capability it costs:

- **Egress guard** (`_assert_public`). luxe runs on a tailnet next to
  privileged mage-hands relays, an oMLX endpoint on localhost, and a NAS.
  A model that can fetch arbitrary URLs can otherwise be talked into
  `http://localhost:8000`, `https://kappa.tailca7308.ts.net/mcp`, or cloud
  metadata at 169.254.169.254 — SSRF against the operator's own fleet. The
  guard resolves the hostname and refuses any non-public address, and it
  re-checks on EVERY redirect hop (a public host can 302 to 127.0.0.1).
- **Size cap**. Read in chunks and stop; a streamed multi-GB response must
  not become a multi-GB string in a chat turn.
- **Time cap**. `httpx` timeouts are per-read, so a trickling response can
  outlive any of them; `_deadline` bounds total wall time as well.

`LUXE_WEB_ALLOW_PRIVATE=1` lifts the egress guard for local development
(e.g. scraping a dev server on localhost). It is deliberately an env var and
not a tool argument — the model must not be able to talk itself past it.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import time
from dataclasses import dataclass, field

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_BYTES = 2_000_000        # 2 MB of source before extraction
DEFAULT_MAX_REDIRECTS = 5
USER_AGENT = "luxe/1.0 (+https://github.com/michaeldtimpe/luxe)"

_ALLOWED_SCHEMES = ("http", "https")


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
    import re

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


def _assert_public(url: str) -> None:
    """Refuse a URL whose host resolves to anything non-public.

    Resolves the name rather than pattern-matching it: `localtest.me` and a
    thousand other public names resolve to 127.0.0.1, so a textual blocklist
    would be theatre.
    """
    from urllib.parse import urlsplit

    url = _normalize_url(url)
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as e:
        raise WebError(f"unparseable URL {url!r}: {e}") from e
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise WebError(
            f"refused scheme `{parts.scheme or '(none)'}` — only http/https "
            "are allowed (no file://, data:, ftp://)")
    if not host:
        raise WebError(f"no host in URL: {url!r}")

    allowlist = _host_allowlist()
    if allowlist:
        import fnmatch
        if not any(fnmatch.fnmatch(host.lower(), pat) for pat in allowlist):
            raise WebError(
                f"refused {host} — not in LUXE_WEB_ALLOWLIST "
                f"({', '.join(allowlist)}). Add a pattern to that env var to "
                "allow it, or unset the variable to allow any public host.")

    if _allow_private():
        return
    try:
        infos = socket.getaddrinfo(host, port or
                                   (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise WebError(f"cannot resolve {host}: {e}") from e
    for info in infos:
        ip = str(info[4][0])
        if not _is_public_ip(ip):
            raise WebError(
                f"refused {host} — it resolves to the non-public address {ip}. "
                "luxe will not fetch private, loopback, or link-local hosts "
                "from a tool (this protects the local oMLX endpoint, the "
                "tailnet relays, and cloud metadata). Set "
                "LUXE_WEB_ALLOW_PRIVATE=1 in the environment if you "
                "deliberately want to scrape a local address.")


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

    req_headers = {"User-Agent": USER_AGENT,
                   "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"}
    req_headers.update(headers or {})

    # Redirects are followed BY HAND so the guard re-runs per hop; httpx's
    # follow_redirects would check only the URL we handed it.
    with httpx.Client(follow_redirects=False, timeout=timeout_s) as client:
        for _hop in range(max_redirects + 1):
            _assert_public(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebError(f"timed out after {timeout_s:.0f}s fetching {url}")
            try:
                with client.stream("GET", current, headers=req_headers,
                                   timeout=remaining) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location", "")
                        if not location:
                            raise WebError(
                                f"{resp.status_code} redirect with no Location header")
                        nxt = str(resp.url.join(location))
                        seen.append(current)
                        current = nxt
                        continue
                    chunks: list[bytes] = []
                    total = 0
                    truncated = False
                    for chunk in resp.iter_bytes():
                        if time.monotonic() > deadline:
                            truncated = True
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= max_bytes:
                            truncated = True
                            break
                    raw = b"".join(chunks)[:max_bytes]
                    encoding = resp.encoding or "utf-8"
                    try:
                        text = raw.decode(encoding, errors="replace")
                    except LookupError:
                        text = raw.decode("utf-8", errors="replace")
                    return FetchResult(
                        url=str(resp.url),
                        status=resp.status_code,
                        content_type=resp.headers.get("content-type", ""),
                        text=text,
                        truncated=truncated,
                        elapsed_s=time.monotonic() - started,
                        redirects=seen,
                    )
            except httpx.HTTPError as e:
                raise WebError(f"{type(e).__name__} fetching {current}: {e}") from e
    raise WebError(f"too many redirects (>{max_redirects}) starting at {url}")
