"""Browser tools (src/luxe/browser.py) — allowlist logic, lazy deps, contracts.

No test launches Chrome or touches the network, and no test depends on
whether the [browser] extra (pychrome/trafilatura) happens to be installed:
the missing-dep condition is FORCED via sys.modules, so the suite passes
identically before and after `uv sync --extra browser`. (An earlier version
trusted the venv to lack the deps; the day they were installed, these tests
launched real Chrome. Force conditions; never assume the environment.)
"""

from __future__ import annotations

import importlib
import sys

import pytest

from luxe import browser
from luxe.browser import (
    DEFAULT_BROWSER_ALLOWLIST,
    _allow_host,
    _load_allowlist_from_env,
    make_browser_tools,
    set_browser_allowlist,
)


@pytest.fixture(autouse=True)
def isolate_module_state(monkeypatch):
    """Tests mutate module-level state (allowlist, tab); reset around each,
    and start from the no-browser-launched baseline."""
    before = browser._ALLOWLIST
    monkeypatch.setattr(browser, "_tab", None)
    monkeypatch.setattr(browser, "_browser", None)
    monkeypatch.setattr(browser, "_chrome_proc", None)
    yield
    browser._ALLOWLIST = before


def _hide_dep(monkeypatch, name):
    """Force `import name` to raise ImportError even when installed:
    a None entry in sys.modules halts the import machinery."""
    monkeypatch.setitem(sys.modules, name, None)


def test_module_imports_cleanly_without_deps(monkeypatch):
    """The whole point of lazy imports: with pychrome/trafilatura absent the
    module must still import (it is imported on every chat turn)."""
    _hide_dep(monkeypatch, "pychrome")
    _hide_dep(monkeypatch, "trafilatura")
    importlib.reload(browser)


def test_make_browser_tools_shape():
    pairs = make_browser_tools()
    assert [d.name for d, _fn in pairs] == ["browse_navigate", "browse_read"]
    for d, fn in pairs:
        assert d.description and d.parameters["type"] == "object"
        assert callable(fn)


def test_navigate_missing_dep_returns_actionable_error(monkeypatch):
    _hide_dep(monkeypatch, "pychrome")
    set_browser_allowlist(("example.com",))
    (nav_def, nav_fn), _ = make_browser_tools()
    out, err = nav_fn({"url": "https://example.com/docs"})
    assert out == ""
    assert err is not None and "uv sync --extra browser" in err


def test_read_missing_dep_returns_actionable_error(monkeypatch):
    # A loaded tab is required before browse_read reaches the trafilatura
    # import; fake one so the missing-dep path is what fires.
    _hide_dep(monkeypatch, "trafilatura")
    monkeypatch.setattr(browser, "_tab", object())
    _, (read_def, read_fn) = make_browser_tools()
    out, err = read_fn({})
    assert out == ""
    assert err is not None and "uv sync --extra browser" in err


def test_read_without_page_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(browser, "_tab", None)
    _, (_d, read_fn) = make_browser_tools()
    out, err = read_fn({})
    assert out == "" and "browse_navigate first" in err


# --- allowlist (pure) --------------------------------------------------------


@pytest.mark.parametrize("url,allowed", [
    ("https://github.com/anthropics/luxe", True),
    ("https://api.github.com/repos", True),          # *.github.com
    ("https://docs.python.org/3/", True),
    ("https://en.wikipedia.org/wiki/SSH", True),     # *.wikipedia.org
    ("https://evil.example.com/", False),
    ("https://github.com.evil.example/", False),     # suffix spoof
    ("http://localhost:8000/", False),
    ("ftp://github.com/", False),                    # not http(s)
    ("not a url", False),
    ("", False),
])
def test_default_allowlist_decisions(url, allowed):
    set_browser_allowlist(DEFAULT_BROWSER_ALLOWLIST)
    verdict = _allow_host(url)
    assert (verdict is None) == allowed
    if not allowed and url.startswith(("http://", "https://")):
        assert "[denied]" in verdict


def test_empty_allowlist_denies_all():
    set_browser_allowlist(())
    verdict = _allow_host("https://github.com/x")
    assert verdict is not None and "[denied]" in verdict and "<empty>" in verdict


def test_custom_patterns_are_case_insensitive_on_host():
    set_browser_allowlist(("*.Internal.Example",))
    assert _allow_host("https://docs.internal.example/page") is None


def test_env_override_parsing(monkeypatch):
    monkeypatch.setenv("LUXE_BROWSER_ALLOWLIST", " a.com , *.b.org ,, c.net ")
    assert _load_allowlist_from_env() == ("a.com", "*.b.org", "c.net")
    monkeypatch.delenv("LUXE_BROWSER_ALLOWLIST")
    assert _load_allowlist_from_env() == DEFAULT_BROWSER_ALLOWLIST


def test_denied_host_never_reaches_chrome(monkeypatch):
    called = {"chrome": False}

    def _boom():
        called["chrome"] = True
        raise AssertionError("Chrome must not launch for a denied host")

    monkeypatch.setattr(browser, "_ensure_chrome", _boom)
    set_browser_allowlist(("github.com",))
    (nav_def, nav_fn), _ = make_browser_tools()
    out, err = nav_fn({"url": "https://evil.example/"})
    assert out == "" and "[denied]" in err
    assert not called["chrome"]


# --- never-raises contract ---------------------------------------------------


def test_browser_tools_never_raise(monkeypatch):
    def _boom():
        raise RuntimeError("devtools exploded")

    monkeypatch.setattr(browser, "_ensure_chrome", _boom)
    set_browser_allowlist(("example.com",))
    (nav_def, nav_fn), (read_def, read_fn) = make_browser_tools()
    out, err = nav_fn({"url": "https://example.com/"})
    assert out == "" and "devtools exploded" in err
    out, err = nav_fn(None)          # even bad args must come back as a tuple
    assert out == "" and err
    out, err = read_fn({})
    assert out == "" and err
