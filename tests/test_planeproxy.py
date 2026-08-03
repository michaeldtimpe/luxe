"""planeproxy diagnostics — pure classifier, probe plumbing, and the chat tool.

No test touches the real network or the real planeproxy binary: subprocess.run
is monkeypatched everywhere a probe would execute, `_resolve_bin` is pinned to
a fake path (or None for the not-installed case), and the classifier runs over
canned JSON payloads shaped like real `status --json` / `doctor --json` output.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from luxe import planeproxy
from luxe.planeproxy import (
    PlaneproxyReport,
    ProbeResult,
    classify,
    full_report,
    make_planeproxy_tool,
    render_lines,
)


# --- canned payloads (shape mirrors real planeproxy output, 2026-08-02) ------

STATUS_UP = {
    "pid": 23638,
    "modes": "api,socks",
    "socks_addr": "127.0.0.1:1080",
    "connect_addr": "127.0.0.1:1081",
    "dns_addr": "127.0.0.1:53",
    "isolation": {
        "enabled": True,
        "allow": ["anthropic.com", "*.anthropic.com"],
        "refused": 44,
        "recent": ["www.example.com"],
    },
    "tunnel": {
        "state": "up",
        "endpoint": "gw.example.com:443",
        "server_key_fingerprint": "SHA256:abc",
        "connected_since": "2026-08-02T21:07:20-05:00",
        "reconnects": 0,
        "bytes_out": 45284266,
        "bytes_in": 2773780,
        "active_conns": 9,
        "total_conns": 344,
        "failed_conns": 4,
    },
}

STATUS_TUNNEL_DOWN = {
    "pid": 23638,
    "modes": "api,socks",
    "tunnel": {"state": "connecting", "endpoint": "gw.example.com:443",
               "reconnects": 3, "last_error": "dial tcp: i/o timeout"},
}

STATUS_NOT_RUNNING = {"running": False}


def _doctor(*checks) -> dict:
    return {"checks": list(checks), "already_up": True}


def _c(name, status, detail="", remedy=""):
    d = {"name": name, "status": status, "detail": detail}
    if remedy:
        d["remedy"] = remedy
    return d


DOCTOR_OK = _doctor(
    _c("configuration", "PASS", "1 host key pin(s)"),
    _c("tunnel key", "PASS"),
    _c("running instance", "PASS", "planeproxy is already up (pid 23638)"),
    _c("system routing", "PASS", "web, dns, ssh routed"),
    _c("reachability gw.example.com:443", "PASS", "TCP connect in 9ms"),
    _c("host key + authentication", "PASS", "host key matches the pin"),
    _c("captive portal", "PASS", "no portal"),
)

DOCTOR_HOSTKEY_MISMATCH = _doctor(
    _c("reachability gw.example.com:443", "PASS"),
    _c("host key + authentication", "FAIL",
       "host key SHA256:evil does NOT match the pin — MISMATCH",
       "do not connect; the network may be intercepting"),
    _c("captive portal", "FAIL", "HTTP 302 → http://portal.example/login"),
)

DOCTOR_PORTAL = _doctor(
    _c("system routing", "PASS"),
    _c("host key + authentication", "PASS"),
    _c("captive portal", "FAIL",
       "HTTP 302 → http://portal.example/login — a portal is answering"),
)

DOCTOR_STRANDED = _doctor(
    _c("system routing", "FAIL",
       "system proxy points at 127.0.0.1:1081 but nothing is listening",
       "run `planeproxy down` to restore direct routing"),
    _c("captive portal", "PASS"),
)

DOCTOR_UNREACHABLE = _doctor(
    _c("system routing", "PASS"),
    _c("reachability gw.example.com:443", "FAIL", "dial tcp: i/o timeout"),
    _c("reachability 5.161.18.200:443", "FAIL", "dial tcp: i/o timeout"),
    _c("captive portal", "PASS"),
)

DOCTOR_AUTH_FAILED = _doctor(
    _c("reachability 5.161.18.200:443", "PASS", "TCP connect in 9ms"),
    _c("host key + authentication", "FAIL",
       "ssh: unable to authenticate, attempted methods [publickey]",
       "check the tunnel key"),
    _c("captive portal", "PASS"),
)


# --- classifier (pure) -------------------------------------------------------


@pytest.mark.parametrize("status_json,doctor_json,verdict", [
    (STATUS_UP, DOCTOR_OK, planeproxy.PP_OK),
    (STATUS_UP, DOCTOR_HOSTKEY_MISMATCH, planeproxy.PP_HOST_KEY_MISMATCH),
    (STATUS_UP, DOCTOR_PORTAL, planeproxy.PP_CAPTIVE_PORTAL),
    (STATUS_NOT_RUNNING, DOCTOR_STRANDED, planeproxy.PP_STRANDED_ROUTING),
    (STATUS_NOT_RUNNING, DOCTOR_UNREACHABLE, planeproxy.PP_UNREACHABLE),
    (STATUS_UP, DOCTOR_AUTH_FAILED, planeproxy.PP_AUTH_FAILED),
    (STATUS_NOT_RUNNING, DOCTOR_OK, planeproxy.PP_NOT_RUNNING),
    (STATUS_TUNNEL_DOWN, DOCTOR_OK, planeproxy.PP_TUNNEL_DOWN),
    (None, None, planeproxy.PP_PROBE_FAILED),
])
def test_classify_table(status_json, doctor_json, verdict):
    assert classify(status_json, doctor_json) == verdict


def test_classify_hostkey_mismatch_beats_captive_portal():
    """Interception evidence outranks everything — a portal FAIL in the same
    doctor payload must not soften the verdict (the mismatch payload above
    deliberately carries a failing portal check too)."""
    fail_names = [c["name"] for c in DOCTOR_HOSTKEY_MISMATCH["checks"]
                  if c["status"] == "FAIL"]
    assert "captive portal" in fail_names  # the precedence is actually exercised
    assert classify(STATUS_UP, DOCTOR_HOSTKEY_MISMATCH) == planeproxy.PP_HOST_KEY_MISMATCH


def test_classify_status_only_still_works():
    """doctor probe missing (check='status' or doctor failed) — the running/
    tunnel checks still classify from status alone."""
    assert classify(STATUS_UP, None) == planeproxy.PP_OK
    assert classify(STATUS_TUNNEL_DOWN, None) == planeproxy.PP_TUNNEL_DOWN
    assert classify(STATUS_NOT_RUNNING, None) == planeproxy.PP_NOT_RUNNING


def test_advice_covers_every_verdict():
    for verdict in (planeproxy.PP_HOST_KEY_MISMATCH, planeproxy.PP_CAPTIVE_PORTAL,
                    planeproxy.PP_STRANDED_ROUTING, planeproxy.PP_UNREACHABLE,
                    planeproxy.PP_AUTH_FAILED, planeproxy.PP_NOT_RUNNING,
                    planeproxy.PP_TUNNEL_DOWN, planeproxy.PP_NOT_INSTALLED,
                    planeproxy.PP_PROBE_FAILED, planeproxy.PP_OK):
        assert planeproxy._ADVICE[verdict]


def test_hostkey_advice_carries_the_doctrine():
    """The stop-not-retry / no-CA / no-bypass doctrine IS the product — pin it."""
    advice = planeproxy._ADVICE[planeproxy.PP_HOST_KEY_MISMATCH]
    assert "STOP" in advice
    assert "CA certificate" in advice
    assert "never disable certificate verification" in advice.lower() or \
        "never disable" in advice.lower()


# --- probe plumbing (_run_json / full_report) --------------------------------


def _fake_run(payloads: dict, exit_codes: dict | None = None):
    """subprocess.run stand-in keyed by subcommand ('status' | 'doctor')."""
    def _run(argv, capture_output=True, text=True, timeout=None):
        subcmd = argv[1]
        class P:
            stdout = json.dumps(payloads[subcmd])
            stderr = ""
            returncode = (exit_codes or {}).get(subcmd, 0)
        return P()
    return _run


@pytest.fixture
def fake_bin(monkeypatch):
    monkeypatch.setattr(planeproxy, "_resolve_bin", lambda: "/fake/planeproxy")


def test_full_report_healthy(fake_bin, monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        _fake_run({"status": STATUS_UP, "doctor": DOCTOR_OK}))
    report = full_report()
    assert report.installed
    assert report.verdict == planeproxy.PP_OK
    assert report.status.ok and report.doctor.ok
    assert report.advice


def test_full_report_doctor_nonzero_exit_is_still_a_successful_probe(fake_bin, monkeypatch):
    """doctor exits 1 when any check FAILs — valid JSON on stdout must be
    parsed, not discarded (the checks carry the news)."""
    monkeypatch.setattr(subprocess, "run",
                        _fake_run({"status": STATUS_UP,
                                   "doctor": DOCTOR_PORTAL},
                                  exit_codes={"doctor": 1}))
    report = full_report()
    assert report.doctor.ok
    assert report.verdict == planeproxy.PP_CAPTIVE_PORTAL


def test_full_report_check_status_only(fake_bin, monkeypatch):
    calls = []

    def _run(argv, **kw):
        calls.append(argv[1])
        class P:
            stdout = json.dumps(STATUS_UP)
            stderr = ""
            returncode = 0
        return P()

    monkeypatch.setattr(subprocess, "run", _run)
    report = full_report(check="status")
    assert calls == ["status"]
    assert report.doctor is None
    assert report.verdict == planeproxy.PP_OK


def test_missing_binary_reports_skipped_not_error(monkeypatch):
    """analysis.py B3: absence must be a structured result, never an error —
    agents read tool errors as false failure signals."""
    monkeypatch.setattr(planeproxy, "_resolve_bin", lambda: None)
    report = full_report()
    assert not report.installed
    assert report.verdict == planeproxy.PP_NOT_INSTALLED

    _defn, fn = make_planeproxy_tool()
    out, err = fn({})
    assert err is None                      # NOT an error
    assert "not-installed" in out.splitlines()[0]
    assert "not installed" in out


def test_timeout_returns_clean_error_tuple(fake_bin, monkeypatch):
    def _hang(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"))

    monkeypatch.setattr(subprocess, "run", _hang)
    data, err = planeproxy._run_json("status")
    assert data is None and "no answer" in err

    report = full_report()
    assert report.verdict == planeproxy.PP_PROBE_FAILED
    _defn, fn = make_planeproxy_tool()
    out, err = fn({})
    assert err is None
    assert "probe-failed" in out.splitlines()[0]


def test_bad_json_reports_raw_head(fake_bin, monkeypatch):
    def _run(argv, **kw):
        class P:
            stdout = "planeproxy: flag provided but not defined" + "x" * 500
            stderr = ""
            returncode = 2
        return P()

    monkeypatch.setattr(subprocess, "run", _run)
    data, err = planeproxy._run_json("doctor")
    assert data is None
    assert "non-JSON" in err
    assert len(err) < 300                     # first ~200 chars only


# --- the chat tool -----------------------------------------------------------


def test_planeproxy_tool_returns_verdict_first(monkeypatch):
    canned = PlaneproxyReport(
        installed=True, bin_path="/fake/planeproxy",
        status=ProbeResult("status", True, STATUS_UP, 12.0),
        doctor=ProbeResult("doctor", True, DOCTOR_PORTAL, 900.0),
        verdict=planeproxy.PP_CAPTIVE_PORTAL,
        advice=planeproxy._ADVICE[planeproxy.PP_CAPTIVE_PORTAL])
    monkeypatch.setattr(planeproxy, "full_report", lambda check="both": canned)
    defn, fn = make_planeproxy_tool()
    assert defn.name == "planeproxy_diag"
    out, err = fn({"check": "both"})
    assert err is None
    lines = out.splitlines()
    assert lines[0] == "verdict: captive-portal"
    assert "captive portal" in lines[1]


def test_planeproxy_tool_never_raises(monkeypatch):
    def _boom(check="both"):
        raise RuntimeError("wires cut")

    monkeypatch.setattr(planeproxy, "full_report", _boom)
    _defn, fn = make_planeproxy_tool()
    out, err = fn({})
    assert out == "" and "wires cut" in err


def test_tool_detail_prunes_noise(monkeypatch):
    """cve_lookup-style compression: PASS details and refused-host lists are
    token noise; failures keep their detail + remedy."""
    canned = PlaneproxyReport(
        installed=True, bin_path="/fake/planeproxy",
        status=ProbeResult("status", True, STATUS_UP, 12.0),
        doctor=ProbeResult("doctor", True, DOCTOR_STRANDED, 900.0),
        verdict=planeproxy.PP_STRANDED_ROUTING,
        advice=planeproxy._ADVICE[planeproxy.PP_STRANDED_ROUTING])
    monkeypatch.setattr(planeproxy, "full_report", lambda check="both": canned)
    _defn, fn = make_planeproxy_tool()
    out, err = fn({})
    assert err is None
    assert "www.example.com" not in out            # isolation.recent pruned
    assert "nothing is listening" in out            # FAIL detail kept
    assert "planeproxy down" in out                 # remedy kept


# --- rendering ---------------------------------------------------------------


def test_render_lines_marks_failures():
    report = PlaneproxyReport(
        installed=True, bin_path="/fake/planeproxy",
        status=ProbeResult("status", True, STATUS_UP, 12.0),
        doctor=ProbeResult("doctor", True, DOCTOR_STRANDED, 900.0),
        verdict=planeproxy.PP_STRANDED_ROUTING,
        advice=planeproxy._ADVICE[planeproxy.PP_STRANDED_ROUTING])
    lines = render_lines(report)
    failed = [text for ok, text in lines if not ok]
    assert any("system routing" in t for t in failed)
    assert any("nothing is listening" in t for t in failed)
    # tunnel + isolation state lines from status
    assert any(t.startswith("tunnel") and "up" in t for _ok, t in lines)
    assert any("isolation enabled" in t and "44 refused" in t for _ok, t in lines)
    # verdict + advice last
    ok_last, last = lines[-1]
    assert not ok_last and last.startswith("verdict: stranded-routing")


def test_render_lines_not_installed():
    report = PlaneproxyReport(installed=False,
                              verdict=planeproxy.PP_NOT_INSTALLED,
                              advice=planeproxy._ADVICE[planeproxy.PP_NOT_INSTALLED])
    lines = render_lines(report)
    assert all(not ok for ok, _ in lines)
    assert "not found" in lines[0][1]
    assert lines[-1][1].startswith("verdict: not-installed")
