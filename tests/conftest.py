"""Shared test fixtures."""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
import threading
from pathlib import Path

import pytest


# --- Hermetic HOME + network guard -------------------------------------------
#
# The suite used to write into the developer's REAL ~/.luxe: `/theme` tests
# left ~/.luxe/theme set to whatever palette they tried last, a modelcaps test
# minted a real session dir every run (half of ~/.luxe/sessions was pytest's),
# and a loop test appended to ~/.luxe/runs/test-escalation/events.jsonl
# forever. Every test now runs with $HOME at its own tmp dir, and one audit
# hook refuses — and fails the test for — any write under the real home or
# any network egress (DNS lookup / connect to a non-loopback host).
#
# The hook is installed once (audit hooks cannot be removed) and is inert
# outside a test body: `_Guard.active` is set only by the autouse fixture.

_REAL_HOME = os.path.expanduser("~")
_REAL_HOME_PREFIXES = tuple(
    {os.path.join(p, "") for p in (_REAL_HOME, os.path.realpath(_REAL_HOME))})
# The checkout itself (and its .venv) may live under $HOME; writing there is
# the normal business of pytest and of the interpreter's bytecode cache.
_REPO_ROOT = os.path.join(os.path.realpath(Path(__file__).resolve().parents[1]), "")

_REAL_BFCL_DATA = os.path.join(_REAL_HOME, ".luxe", "bfcl-data")

# Modules that bind a ~-derived path at IMPORT time. A module first imported
# inside a test binds the (already redirected) tmp HOME; one imported earlier
# — at collection — still holds the real path, so the fixture repoints it.
# Anything missed here is caught by the write guard below, not silently leaked.
_IMPORT_TIME_HOME_PATHS = (
    ("luxe.chat.theme", "_PREF_PATH", (".luxe", "theme")),
    ("luxe.secrets", "SECRETS_PATH", (".luxe", "secrets.env")),
    ("luxe.tools.cve_lookup", "_CACHE_DIR", (".luxe", "cve_cache")),
    ("luxe.agents.cohort_priors", "DEFAULT_COHORT_HISTORY_DIR",
     (".luxe", "cohort-history")),
    ("luxe.modelstore", "DEFAULT_MODELS_DIR", (".omlx", "models")),
)


class _Guard:
    active = False
    allow_network = False
    violations: list[str] = []
    # Per-thread reentrancy latch: the hook's own work must not re-enter it,
    # but one thread inside the hook must not blind it for the others.
    _local = threading.local()


def _under_real_home(path) -> bool:
    if isinstance(path, int):
        return False
    try:
        p = os.path.abspath(os.fsdecode(path))
    except (TypeError, ValueError):
        return False
    p = os.path.join(p, "")
    return p.startswith(_REAL_HOME_PREFIXES) and not p.startswith(_REPO_ROOT)


def _is_local_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if not isinstance(host, str):
        return True
    if host == "" or host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False  # a name that would need DNS
    return ip.is_loopback or ip.is_unspecified


_FS_EVENTS = {
    "os.mkdir": (0,), "os.remove": (0,), "os.rmdir": (0,),
    "shutil.rmtree": (0,), "os.rename": (0, 1), "os.replace": (0, 1),
    "os.symlink": (1,), "os.link": (1,), "os.truncate": (0,),
}


def _refuse(msg: str, exc: type[BaseException]) -> None:
    _Guard.violations.append(msg)
    raise exc(msg)


def _audit(event: str, args: tuple) -> None:
    if not _Guard.active or getattr(_Guard._local, "busy", False):
        return
    _Guard._local.busy = True
    try:
        if event == "open":
            path, mode, flags = args
            writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int)
                and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
            if writes and _under_real_home(path):
                _refuse(f"write under the real home: {path}", PermissionError)
        elif event in _FS_EVENTS:
            for i in _FS_EVENTS[event]:
                if _under_real_home(args[i]):
                    _refuse(f"{event} under the real home: {args[i]}", PermissionError)
        elif _Guard.allow_network:
            pass
        elif event == "socket.getaddrinfo":
            if not _is_local_host(args[0]):
                _refuse(f"DNS lookup of {args[0]!r}", ConnectionRefusedError)
        elif event == "socket.connect":
            sock, addr = args
            if (getattr(sock, "family", None) in (socket.AF_INET, socket.AF_INET6)
                    and not _is_local_host(addr[0])):
                _refuse(f"connect to {addr!r}", ConnectionRefusedError)
    finally:
        _Guard._local.busy = False


sys.addaudithook(_audit)


# TEMPORARY: these tests resolve public hostnames today; a separate change
# makes them hermetic. Remove each entry when that lands — the guard then
# covers them too.
_TEMP_LIVE_NETWORK = (
    "tests/test_web.py::test_allowlist_when_set_is_deny_by_default",
    "tests/test_web_page.py::TestActions::test_click_by_css_selector_passes_through",
    "tests/test_web_page.py::TestActions::test_click_by_index_uses_the_tagged_selector",
    "tests/test_web_page.py::TestActions::test_missing_target_is_a_clean_error",
    "tests/test_web_page.py::TestActions::test_type_with_submit_presses_enter",
    "tests/test_web_page.py::TestEgress::test_navigation_to_private_space_hard_closes_the_session",
    "tests/test_web_page.py::TestOwnership::test_close_is_idempotent_and_stops_the_driver",
    "tests/test_web_page.py::TestOwnership::test_ops_from_different_threads_share_one_driver",
    "tests/test_web_page.py::TestRendering::test_a_full_listing_says_it_may_have_been_cut",
    "tests/test_web_page.py::TestRendering::test_a_short_listing_is_unannotated",
    "tests/test_web_page.py::TestRendering::test_snapshot_renders_state_content_and_interactables",
)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == "call":
        item._luxe_call_failed = report.failed
    return report


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.nodeid in _TEMP_LIVE_NETWORK:
            item.add_marker(pytest.mark.live_network)


@pytest.fixture
def sandbox_guard():
    """The guard state, for tests that pin the sandbox itself."""
    return _Guard


@pytest.fixture(autouse=True)
def _hermetic_home(request, tmp_path_factory, monkeypatch):
    """Point $HOME (and every import-time ~ path) at a per-test tmp dir, and
    refuse real-home writes and network egress for the test body."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    # A moved HOME hides the developer's ~/.gitconfig (identity, signing,
    # default branch). Give git a fixed, unsigned one so tests that commit
    # behave the same on every box; repo-local config still wins over it.
    (home / ".gitconfig").write_text(
        "[user]\n\tname = luxe-test\n\temail = luxe-test@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n"
        "[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n")
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    # Vendored benchmark DATA is the one thing under the real home tests may
    # READ (the BFCL loader defaults to ~/.luxe/bfcl-data); keep them on it.
    if "LUXE_BFCL_DATA_DIR" not in os.environ and os.path.isdir(_REAL_BFCL_DATA):
        monkeypatch.setenv("LUXE_BFCL_DATA_DIR", _REAL_BFCL_DATA)
    for mod_name, attr, parts in _IMPORT_TIME_HOME_PATHS:
        mod = sys.modules.get(mod_name)
        if mod is not None:
            monkeypatch.setattr(mod, attr, home.joinpath(*parts))

    _Guard.violations = []
    _Guard.allow_network = request.node.get_closest_marker("live_network") is not None
    _Guard.active = True
    try:
        yield home
    finally:
        _Guard.active = False
    # A refusal raised inside the test body already failed it; only report
    # here when the test swallowed the refusal (e.g. `except OSError: pass`).
    if _Guard.violations and not getattr(request.node, "_luxe_call_failed", False):
        pytest.fail("test escaped its sandbox:\n  " + "\n  ".join(
            dict.fromkeys(_Guard.violations)), pytrace=False)


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Create a minimal repo structure for testing."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n\n'
        'def add(a: int, b: int) -> int:\n    return a + b\n'
    )
    (tmp_path / "src" / "utils.py").write_text(
        'import os\n\ndef get_env(key: str) -> str:\n    return os.environ.get(key, "")\n'
    )
    (tmp_path / "README.md").write_text("# Test Repo\n\nA test repository.\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\nversion = "0.1.0"\n')
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_tool_sizing_globals():
    """`tools/fs.py`'s sizing toggles are PROCESS-GLOBAL, and since chat's read
    budget went default-ON (2026-08-24,
    `acceptance/chat_bigread_2026_08_24/REPORT.md`) every `prepare_turn` call in
    the suite leaves one set — which is how a chat-seam test silently moved
    `read_limit()` under a later `test_read_file_large.py` assertion about the
    fixed 256 KB cap. Reset after each test so module order stays irrelevant."""
    yield
    from luxe.tools import fs as _fs
    _fs.set_read_budget(None)
    _fs.set_large_file_notes(False)


@pytest.fixture
def config_path() -> Path:
    return Path(__file__).parent.parent / "configs" / "single_64gb.yaml"
