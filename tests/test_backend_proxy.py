"""A loopback backend must never go through a proxy (2026-08-23).

A planeproxy tunnel that outlived its SSH session left `127.0.0.1:1081` in the
macOS System Configuration proxy settings. httpx trusts the environment by
default, and on macOS that means urllib's `getproxies_macosx_sysconf()` — which
silently DROPS the ExceptionsList that would have spared loopback (httpx only
honours a "no" key the macOS reader never returns). So every request to the
LOCAL oMLX endpoint was posted into a dead port; the ConnectError was contained
into an aborted turn and `luxe chat` answered five times with nothing
(~/.luxe/sessions/3aabb18b0e07). No model, no network.
"""

from __future__ import annotations

import httpx
import pytest

from luxe import netdiag
from luxe.backend import Backend, is_loopback_url
from luxe.modelstore import OmlxAdmin

DEAD_PROXY = "http://127.0.0.1:1081"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000",
    "http://127.0.0.5:8000",        # all of 127.0.0.0/8, not just .1
    "http://localhost:8000",
    "https://LOCALHOST:8000",       # host comparison is case-insensitive
    "http://[::1]:8000",
    "http://0.0.0.0:8000",
])
def test_loopback_urls_recognised(url):
    assert is_loopback_url(url)


@pytest.mark.parametrize("url", [
    "http://m5.tailnet.ts.net:8000",
    "https://openrouter.ai/api/v1",
    "http://192.168.1.10:8000",     # LAN is not loopback — it is a network hop
    "",                             # no host: NOT positively loopback
])
def test_non_loopback_urls_not_recognised(url):
    assert not is_loopback_url(url)


def test_local_backend_ignores_a_stranded_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", DEAD_PROXY)
    b = Backend(base_url="http://127.0.0.1:8000", model="m", api_key="k")
    assert b._client.trust_env is False
    # …and no proxy transport was mounted, which is what trust_env buys us.
    assert not b._client._mounts


def test_remote_backend_still_trusts_the_environment(monkeypatch):
    """The operator deliberately routes REMOTE traffic through planeproxy."""
    monkeypatch.setenv("HTTPS_PROXY", DEAD_PROXY)
    b = Backend(base_url="https://openrouter.ai/api/v1", model="m", api_key="k")
    assert b._client.trust_env is True
    assert b._client._mounts


def test_omlx_admin_skips_the_proxy_on_loopback(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", DEAD_PROXY)
    with OmlxAdmin(api_key="k") as admin:          # defaults to 127.0.0.1:8000
        assert admin._client.trust_env is False


def test_endpoint_probe_skips_the_proxy_on_loopback(monkeypatch):
    """`/doctor` must report the ENDPOINT's health, not a stale proxy's. The
    rungs above it (`probe_http`, `probe_portal`) keep trust_env on purpose —
    they are measuring the network the environment describes."""
    seen: dict = {}

    def fake_get(url, **kw):
        seen.clear()
        seen.update(kw)
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    netdiag.probe_endpoint("local", "http://127.0.0.1:8000")
    assert seen["trust_env"] is False

    netdiag.probe_endpoint("m5", "http://m5.tailnet.ts.net:8000")
    assert seen["trust_env"] is True
