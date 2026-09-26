"""The suite's sandbox (tests/conftest.py) must actually hold.

Before it existed, `/theme` tests rewrote the developer's ~/.luxe/theme, a
modelcaps test minted a real session dir on every run, and a loop test
appended to ~/.luxe/runs/test-escalation/events.jsonl without end. These pin
the three pieces that prevent that: $HOME is a per-test tmp dir, the modules
that bind a ~ path at import follow it, and anything that still reaches for
the real home or the network is refused.
"""

from __future__ import annotations

import importlib
import os
import socket
import uuid
from pathlib import Path

import pytest

from luxe.paths import luxe_home


def _real_home() -> str:
    """The account's home from the password database — immune to $HOME."""
    import pwd
    return pwd.getpwuid(os.getuid()).pw_dir


def test_home_is_a_per_test_tmp_dir(sandbox_guard, _hermetic_home):
    assert Path.home() == _hermetic_home
    assert luxe_home() == _hermetic_home / ".luxe"
    assert sandbox_guard.active


@pytest.mark.parametrize("mod_name, attr, parts", [
    ("luxe.chat.theme", "_PREF_PATH", (".luxe", "theme")),
    ("luxe.secrets", "SECRETS_PATH", (".luxe", "secrets.env")),
    ("luxe.tools.cve_lookup", "_CACHE_DIR", (".luxe", "cve_cache")),
    ("luxe.agents.cohort_priors", "DEFAULT_COHORT_HISTORY_DIR",
     (".luxe", "cohort-history")),
    ("luxe.modelstore", "DEFAULT_MODELS_DIR", (".omlx", "models")),
])
def test_import_time_home_paths_follow_the_moved_home(mod_name, attr, parts):
    """These are bound at import — collection time, under the REAL home."""
    mod = importlib.import_module(mod_name)
    assert str(Path.home()) != _real_home()
    assert getattr(mod, attr) == Path.home().joinpath(*parts)


def test_a_write_under_the_real_home_is_refused(sandbox_guard):
    target = os.path.join(_real_home(), ".luxe", f"pytest-guard-{uuid.uuid4().hex}")
    with pytest.raises(PermissionError, match="real home"):
        open(target, "w")
    assert not os.path.exists(target)
    sandbox_guard.violations.clear()  # refused as intended; don't fail teardown


def test_a_dns_lookup_is_refused(sandbox_guard):
    with pytest.raises(ConnectionRefusedError, match="DNS lookup"):
        socket.getaddrinfo("example.com", 443)
    sandbox_guard.violations.clear()


def test_a_non_loopback_connect_is_refused(sandbox_guard):
    s = socket.socket()
    try:
        with pytest.raises(ConnectionRefusedError, match="connect to"):
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: never routable anyway
    finally:
        s.close()
    sandbox_guard.violations.clear()


def test_loopback_stays_allowed():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    try:
        socket.create_connection(srv.getsockname(), timeout=2).close()
    finally:
        srv.close()
