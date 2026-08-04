"""Regression tests for the stale-brew-process detector (src/luxe/staleproc.py).

The bug it exists for cost real debugging time twice in two days
(lessons.md 2026-08-03 and 2026-08-04): a brew-managed service left running
across `brew upgrade` executes from a deleted Cellar tree, passes every
health check, and then fails one lazy import with an error naming whatever
module it happened to reach for.

Everything here is simulated — no brew, no lsof, no running service — so the
tests assert the same verdicts on any box.
"""

from __future__ import annotations

import pytest

from luxe import staleproc
from luxe.staleproc import StaleCheck, check_stale_service


@pytest.fixture()
def fake(monkeypatch):
    """Drive the detector from a scripted host state."""
    state = {"installed": ("0.5.7",), "pid": 4242, "lsof": "", "started": None,
             "ctime": {}}

    monkeypatch.setattr(staleproc, "_installed_versions",
                        lambda formula: state["installed"])
    monkeypatch.setattr(staleproc, "_pid_for", lambda pattern: state["pid"])

    def _run(argv):
        if argv[0] == "lsof":
            return state["lsof"]
        return ""
    monkeypatch.setattr(staleproc, "_run", _run)
    monkeypatch.setattr(staleproc, "_process_start_epoch",
                        lambda pid: state["started"])
    return state


def _lsof_output(*paths: str) -> str:
    """`lsof -Fn` emits one `n<path>` record per open file."""
    return "p4242\n" + "".join(f"f3\nn{p}\n" for p in paths)


# --- the case this module exists for --------------------------------------

def test_detects_the_2026_08_04_condition(fake):
    """Running 0.5.5 after brew installed 0.5.7 — the real incident."""
    fake["installed"] = ("0.5.7",)
    fake["lsof"] = _lsof_output(
        "/opt/homebrew/Cellar/omlx/0.5.5/libexec/lib/python3.11/site-packages/certifi/cacert.pem",
        "/opt/homebrew/Cellar/omlx/0.5.5/libexec/bin/python3.11",
    )
    got = check_stale_service("omlx", "omlx-server")
    assert got.conclusive is True
    assert got.stale is True
    assert got.basis == "lsof"
    assert got.running_versions == ("0.5.5",)
    assert got.installed_versions == ("0.5.7",)
    assert got.pid == 4242
    # the detail must name both versions — that comparison IS the diagnosis
    assert "0.5.5" in got.detail and "0.5.7" in got.detail
    assert got.fix == "`brew services restart omlx`"


def test_healthy_process_is_not_stale(fake):
    fake["installed"] = ("0.5.7",)
    fake["lsof"] = _lsof_output(
        "/opt/homebrew/Cellar/omlx/0.5.7/libexec/bin/python3.11")
    got = check_stale_service("omlx", "omlx-server")
    assert got.conclusive is True
    assert got.stale is False
    assert got.fix == ""
    assert "matches installed" in got.detail


def test_multiple_installed_versions_one_of_which_is_running(fake):
    """`brew` can keep old kegs. Running any INSTALLED version is fine."""
    fake["installed"] = ("0.5.5", "0.5.7")
    fake["lsof"] = _lsof_output("/opt/homebrew/Cellar/omlx/0.5.5/libexec/bin/python")
    got = check_stale_service("omlx", "omlx-server")
    assert got.conclusive is True
    assert got.stale is False


def test_other_formulas_in_lsof_are_ignored(fake):
    """A process holding some other formula's files open is not evidence."""
    fake["installed"] = ("0.5.7",)
    fake["lsof"] = _lsof_output(
        "/opt/homebrew/Cellar/openssl@3/3.2.1/lib/libssl.dylib",
        "/opt/homebrew/Cellar/omlx/0.5.7/libexec/bin/python3.11",
    )
    got = check_stale_service("omlx", "omlx-server")
    assert got.stale is False
    assert got.running_versions == ("0.5.7",)


# --- inconclusive states must NOT read as healthy -------------------------

def test_no_running_process_is_inconclusive(fake):
    fake["pid"] = None
    got = check_stale_service("omlx", "omlx-server")
    assert got.conclusive is False
    assert got.stale is False
    assert "no running omlx process" in got.reason


def test_formula_not_installed_is_inconclusive(fake):
    fake["installed"] = ()
    got = check_stale_service("omlx", "omlx-server")
    assert got.conclusive is False
    assert "not brew-installed" in got.reason


def test_lsof_and_ps_both_silent_is_inconclusive(fake):
    fake["lsof"] = ""
    fake["started"] = None
    got = check_stale_service("omlx", "omlx-server")
    assert got.conclusive is False
    assert got.stale is False
    assert "could not inspect" in got.reason


# --- mtime fallback when lsof gives nothing -------------------------------

def test_mtime_fallback_flags_a_tree_created_after_the_process_started(
        fake, monkeypatch, tmp_path):
    """No lsof: a Cellar tree created AFTER the process started cannot be the
    one it is running."""
    cellar = tmp_path / "Cellar" / "omlx" / "0.5.7"
    cellar.mkdir(parents=True)
    monkeypatch.setattr(staleproc, "_brew_prefix", lambda: str(tmp_path))
    fake["installed"] = ("0.5.7",)
    fake["lsof"] = ""
    fake["started"] = cellar.stat().st_ctime - 3600      # started an hour before
    got = check_stale_service("omlx", "omlx-server")
    assert got.conclusive is True
    assert got.basis == "mtime"
    assert got.stale is True


def test_mtime_fallback_accepts_a_process_started_after_the_tree(
        fake, monkeypatch, tmp_path):
    cellar = tmp_path / "Cellar" / "omlx" / "0.5.7"
    cellar.mkdir(parents=True)
    monkeypatch.setattr(staleproc, "_brew_prefix", lambda: str(tmp_path))
    fake["installed"] = ("0.5.7",)
    fake["lsof"] = ""
    fake["started"] = cellar.stat().st_ctime + 3600      # started an hour after
    got = check_stale_service("omlx", "omlx-server")
    assert got.conclusive is True
    assert got.basis == "mtime"
    assert got.stale is False


def test_lsof_wins_over_the_mtime_heuristic(fake, monkeypatch, tmp_path):
    """When lsof answers, the timing heuristic must not get a vote — a box
    where brew re-created the tree without an upgrade would false-positive."""
    cellar = tmp_path / "Cellar" / "omlx" / "0.5.7"
    cellar.mkdir(parents=True)
    monkeypatch.setattr(staleproc, "_brew_prefix", lambda: str(tmp_path))
    fake["installed"] = ("0.5.7",)
    fake["lsof"] = _lsof_output("/opt/homebrew/Cellar/omlx/0.5.7/libexec/bin/python")
    fake["started"] = cellar.stat().st_ctime - 3600      # would say "stale"
    got = check_stale_service("omlx", "omlx-server")
    assert got.basis == "lsof"
    assert got.stale is False


# --- never raises ---------------------------------------------------------

def test_every_subprocess_blowing_up_is_survivable(monkeypatch):
    """doctor and smoke both run during outages; a diagnostic may not take
    them down. `_run` is the boundary that swallows this."""
    def boom(*a, **kw):
        raise OSError("no such binary")
    monkeypatch.setattr(staleproc.subprocess, "run", boom)
    got = staleproc.check_stale_service("omlx", "omlx-server")
    assert isinstance(got, StaleCheck)
    assert got.conclusive is False
    assert got.stale is False


def test_an_unexpected_error_still_returns_a_verdict(monkeypatch):
    """Belt and braces: anything the helpers can throw is contained here, not
    left to the two call sites to remember."""
    def boom(formula):
        raise RuntimeError("something nobody predicted")
    monkeypatch.setattr(staleproc, "_installed_versions", boom)
    got = staleproc.check_stale_service("omlx", "omlx-server")
    assert got.conclusive is False
    assert got.stale is False
    assert "check errored" in got.reason


def test_real_run_helper_survives_a_missing_binary():
    assert staleproc._run(["definitely-not-a-real-binary-xyz"]) == ""


def test_check_omlx_returns_a_verdict_on_this_box():
    """Whatever this machine's state, the call completes and is well-formed."""
    got = staleproc.check_omlx()
    assert got.formula == "omlx"
    assert isinstance(got.conclusive, bool)
    assert isinstance(got.stale, bool)
    if not got.conclusive:
        assert got.reason
        assert got.stale is False


# --- wiring ---------------------------------------------------------------

def test_doctor_reports_a_stale_build(monkeypatch):
    from luxe.chat import inspection

    monkeypatch.setattr("luxe.chat.origin.endpoint_is_local", lambda url: True)
    monkeypatch.setattr(staleproc, "check_omlx", lambda: StaleCheck(
        formula="omlx", conclusive=True, stale=True, pid=99,
        running_versions=("0.5.5",), installed_versions=("0.5.7",),
        basis="lsof"))
    doc = inspection.Doctor()
    inspection._add_stale_check(doc, "http://127.0.0.1:8000")
    check = next(c for c in doc.checks if c.name == "oMLX build")
    assert check.state == inspection.WARN
    assert "0.5.5" in check.detail and "0.5.7" in check.detail
    assert "brew services restart omlx" in check.fix


def test_doctor_is_silent_for_a_remote_endpoint(monkeypatch):
    """A remote host's process table is its own doctor's problem."""
    from luxe.chat import inspection

    monkeypatch.setattr("luxe.chat.origin.endpoint_is_local", lambda url: False)
    monkeypatch.setattr(staleproc, "check_omlx",
                        lambda: pytest.fail("must not probe for a remote endpoint"))
    doc = inspection.Doctor()
    inspection._add_stale_check(doc, "http://m5.example:8000")
    assert [c for c in doc.checks if c.name == "oMLX build"] == []


def test_doctor_is_silent_when_inconclusive(monkeypatch):
    from luxe.chat import inspection

    monkeypatch.setattr("luxe.chat.origin.endpoint_is_local", lambda url: True)
    monkeypatch.setattr(staleproc, "check_omlx", lambda: StaleCheck(
        formula="omlx", conclusive=False, reason="no running omlx process"))
    doc = inspection.Doctor()
    inspection._add_stale_check(doc, "http://127.0.0.1:8000")
    assert [c for c in doc.checks if c.name == "oMLX build"] == []


def test_doctor_never_raises_from_this_check(monkeypatch):
    from luxe.chat import inspection

    monkeypatch.setattr("luxe.chat.origin.endpoint_is_local", lambda url: True)

    def boom():
        raise RuntimeError("lsof exploded")
    monkeypatch.setattr(staleproc, "check_omlx", boom)
    doc = inspection.Doctor()
    inspection._add_stale_check(doc, "http://127.0.0.1:8000")
    check = next(c for c in doc.checks if c.name == "oMLX build")
    assert check.state == inspection.OK          # a broken probe is not a fault
    assert "unchecked" in check.detail


def test_stale_build_is_a_warn_not_a_fail_in_doctor(monkeypatch):
    """A stale process may still be serving everything asked of it. The real
    checks fail on their own if it isn't; this line only explains them."""
    from luxe.chat import inspection

    monkeypatch.setattr("luxe.chat.origin.endpoint_is_local", lambda url: True)
    monkeypatch.setattr(staleproc, "check_omlx", lambda: StaleCheck(
        formula="omlx", conclusive=True, stale=True, pid=1,
        running_versions=("0.5.5",), installed_versions=("0.5.7",),
        basis="lsof"))
    doc = inspection.Doctor()
    inspection._add_stale_check(doc, "http://127.0.0.1:8000")
    assert doc.worst == inspection.WARN
