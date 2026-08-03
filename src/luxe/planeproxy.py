"""planeproxy diagnostics — bounded, read-only probes of the SSH-tunnel tool.

planeproxy is the user's Go tunnel for hostile networks (in-flight Wi-Fi,
conference NAT, hotel captive portals): SSH to a fixed endpoint on :443,
local SOCKS/CONNECT proxies, optional system routing + isolation mode.
When it misbehaves, the model's instinct is `ssh -v`, `ps`, `lsof`, and
log-reading — unbounded, noisy, and occasionally destructive. This module
is the purpose-built instrument: it runs planeproxy's OWN read-only
subcommands (`status --json`, `doctor --json`) under a hard deadline and
ends in a VERDICT that names the failure and the fix.

Three surfaces share it (mirroring netdiag.py):
  - `planeproxy_diag` tool (chat-only, via the extra-tool seam) — one
    bounded call instead of improvised shell forensics;
  - `/planeproxy` in chat — deterministic report, no model in the loop;
  - `luxe planeproxy` CLI — same report + exit code for scripts.

Design rules:
  - Bounded: every subprocess call carries `_TIMEOUT_S`; a hang becomes an
    error string, never a stuck surface.
  - Never raises: probe failures, missing binary, bad JSON — all are data
    in the report. A missing binary is a structured "not installed" result,
    NOT an error (tools/analysis.py B3: agents read errors as false signals).
  - READ-ONLY. This module runs `status` and `doctor` and nothing else — it
    must never invoke `up`/`down`/`on`/`off` or any mutating subcommand.
    Doctrine it must carry, verbatim in the advice strings: a host-key
    mismatch is a STOP (interception), never a retry; never install a
    network's CA certificate; never disable certificate verification.
  - Pure logic + dataclasses here; rich rendering stays in the surfaces
    (commands.py / cli.py). `classify()` is pure and unit-tested against
    canned JSON (tests/test_planeproxy.py).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# One deadline for each subcommand run. doctor performs its own bounded
# network checks (TCP dial + SSH auth) and finishes in a few seconds; 15s is
# generous slack, and a breach means planeproxy itself is wedged — a finding.
_TIMEOUT_S = 15.0


def _resolve_bin() -> str | None:
    """The planeproxy binary: PATH first, then the documented install spot."""
    found = shutil.which("planeproxy")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "planeproxy"
    if fallback.is_file():
        return str(fallback)
    return None


def binary_present() -> bool:
    """Cheap presence check for conditional prompt hints (one stat)."""
    return _resolve_bin() is not None


def _run_json(subcmd: str) -> tuple[dict | None, str]:
    """Run `planeproxy <subcmd> --json`, return (parsed, error).

    Missing binary → (None, "") — the CALLER reports "not installed" as a
    structured verdict, never as an error string (analysis.py `_skipped`
    convention: an error here would read as a false failure signal).
    Note: `doctor` exits 1 when any check FAILs — valid JSON on stdout with
    a nonzero exit is still a SUCCESSFUL probe; the checks carry the news.
    """
    bin_path = _resolve_bin()
    if bin_path is None:
        return None, ""
    try:
        proc = subprocess.run(
            [bin_path, subcmd, "--json"],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None, f"planeproxy {subcmd} gave no answer in {_TIMEOUT_S:.0f}s"
    except OSError as e:
        return None, f"planeproxy {subcmd} failed to run: {type(e).__name__}: {e}"
    raw = (proc.stdout or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        head = raw[:200] if raw else (proc.stderr or "").strip()[:200]
        return None, f"planeproxy {subcmd} printed non-JSON: {head!r}"
    if not isinstance(data, dict):
        return None, f"planeproxy {subcmd} printed unexpected JSON: {raw[:200]!r}"
    return data, ""


# Verdicts, most-broken first. classify() returns the first that matches.
PP_HOST_KEY_MISMATCH = "host-key-mismatch"
PP_CAPTIVE_PORTAL = "captive-portal"
PP_STRANDED_ROUTING = "stranded-routing"
PP_UNREACHABLE = "endpoint-unreachable"
PP_AUTH_FAILED = "auth-failed"
PP_NOT_RUNNING = "not-running"
PP_TUNNEL_DOWN = "tunnel-down"
PP_NOT_INSTALLED = "not-installed"
PP_PROBE_FAILED = "probe-failed"
PP_OK = "ok"

_ADVICE = {
    PP_HOST_KEY_MISMATCH: (
        "STOP. The network is presenting a different host key — treat as "
        "interception. Never install a network's CA certificate and never "
        "disable certificate verification; verify aurora's key out of band "
        "before using the tunnel."
    ),
    PP_CAPTIVE_PORTAL: (
        "a captive portal is holding the network: sign in at "
        "http://captive.apple.com first; if 'system routing' also fails, run "
        "`planeproxy down` before the sign-in so the browser can reach the "
        "portal."
    ),
    PP_STRANDED_ROUTING: (
        "system proxy/DNS point at planeproxy but nothing carries them — run "
        "`planeproxy down` to restore direct routing."
    ),
    PP_UNREACHABLE: (
        "no tunnel endpoint is reachable — the network may block outbound "
        ":443; classify the link first (`luxe net` / net_probe) before "
        "retrying `planeproxy up`."
    ),
    PP_AUTH_FAILED: (
        "the endpoint answered but SSH authentication failed — check the "
        "tunnel key `planeproxy doctor` names; never work around this by "
        "weakening or disabling verification."
    ),
    PP_NOT_RUNNING: (
        "the network looks workable — `plane on` (or `planeproxy up`) starts "
        "the tunnel."
    ),
    PP_TUNNEL_DOWN: (
        "planeproxy is running but the tunnel is not up — see tunnel.last_error "
        "in status; re-check in a moment, and if it stays down run "
        "`planeproxy doctor` (read-only) for the failing layer."
    ),
    PP_NOT_INSTALLED: (
        "planeproxy is not installed on this machine (not on PATH, not at "
        "~/.local/bin/planeproxy) — nothing to diagnose."
    ),
    PP_PROBE_FAILED: (
        "the planeproxy binary is present but its probes failed (timeout or "
        "unparseable output) — run `planeproxy status` by hand to see what it "
        "prints."
    ),
    PP_OK: "tunnel up and doctor checks pass — planeproxy is healthy.",
}


@dataclass
class ProbeResult:
    subcmd: str              # "status" | "doctor"
    ok: bool
    data: dict | None
    ms: float
    error: str = ""          # "" when ok


@dataclass
class PlaneproxyReport:
    installed: bool
    bin_path: str = ""
    status: ProbeResult | None = None
    doctor: ProbeResult | None = None
    verdict: str = PP_OK
    advice: str = ""


# --- classification (pure) ---------------------------------------------------


def _checks(doctor_json: dict | None) -> list[dict]:
    if not doctor_json:
        return []
    checks = doctor_json.get("checks")
    return [c for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []


def _check_named(doctor_json: dict | None, name: str) -> dict | None:
    return next((c for c in _checks(doctor_json)
                 if c.get("name") == name), None)


def _failed(check: dict | None) -> bool:
    return check is not None and check.get("status") == "FAIL"


def _is_running(status_json: dict | None) -> bool:
    """`status --json` prints {"running": false} when down; a live instance
    prints a pid (and no "running" key)."""
    return bool(status_json) and bool(status_json.get("pid"))


def classify(status_json: dict | None, doctor_json: dict | None) -> str:
    """Decision tree over the two probe payloads, most-broken verdict first.
    PURE — no I/O; unit-tested against canned JSON. Precedence: host-key
    mismatch outranks everything (interception evidence beats every other
    symptom), captive portal next (it EXPLAINS most downstream failures),
    then stranded routing, unreachable, auth, not-running, tunnel-down, ok.
    """
    if status_json is None and doctor_json is None:
        return PP_PROBE_FAILED

    hostkey = _check_named(doctor_json, "host key + authentication")
    if _failed(hostkey):
        text = (str(hostkey.get("detail", "")) + " "
                + str(hostkey.get("remedy", ""))).lower()
        if ("mismatch" in text or "does not match" in text
                or "different host key" in text or "intercept" in text):
            return PP_HOST_KEY_MISMATCH

    if _failed(_check_named(doctor_json, "captive portal")):
        return PP_CAPTIVE_PORTAL

    if _failed(_check_named(doctor_json, "system routing")):
        return PP_STRANDED_ROUTING

    reach = [c for c in _checks(doctor_json)
             if str(c.get("name", "")).startswith("reachability")]
    if reach and all(c.get("status") == "FAIL" for c in reach):
        return PP_UNREACHABLE

    if _failed(hostkey):  # FAIL without mismatch language = auth trouble
        return PP_AUTH_FAILED

    if not _is_running(status_json):
        return PP_NOT_RUNNING

    tunnel = (status_json or {}).get("tunnel") or {}
    if tunnel.get("fatal") or tunnel.get("state") != "up":
        return PP_TUNNEL_DOWN

    return PP_OK


# --- full report -------------------------------------------------------------


def _probe(subcmd: str) -> ProbeResult:
    t0 = time.monotonic()
    data, err = _run_json(subcmd)
    ms = (time.monotonic() - t0) * 1000.0
    return ProbeResult(subcmd, ok=data is not None, data=data, ms=ms, error=err)


def full_report(check: str = "both") -> PlaneproxyReport:
    """Run the requested probes and classify. `check` is status|doctor|both.
    Never raises; a missing binary is the `not-installed` verdict, and a
    probe failure with the binary present is `probe-failed` — an error must
    not masquerade as absence."""
    bin_path = _resolve_bin()
    if bin_path is None:
        report = PlaneproxyReport(installed=False, verdict=PP_NOT_INSTALLED)
        report.advice = _ADVICE[PP_NOT_INSTALLED]
        return report

    check = check if check in ("status", "doctor", "both") else "both"
    status = _probe("status") if check in ("status", "both") else None
    doctor = _probe("doctor") if check in ("doctor", "both") else None

    report = PlaneproxyReport(installed=True, bin_path=bin_path,
                              status=status, doctor=doctor)
    ran = [p for p in (status, doctor) if p is not None]
    if ran and all(not p.ok for p in ran):
        report.verdict = PP_PROBE_FAILED
    else:
        report.verdict = classify(
            status.data if status else None,
            doctor.data if doctor else None,
        )
    report.advice = _ADVICE[report.verdict]
    return report


# --- rendering (pure text; surfaces apply glyphs/colour) ---------------------


def _human_bytes(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def render_lines(report: PlaneproxyReport) -> list[tuple[bool, str]]:
    """(ok, text) pairs for the surfaces to glyph/colour — pure text here,
    rich markup belongs to the renderer (commands.py / cli.py). Doctor
    checks first, then tunnel + isolation state, verdict + advice last."""
    lines: list[tuple[bool, str]] = []
    if not report.installed:
        lines.append((False, "binary   planeproxy not found on PATH or "
                             "~/.local/bin — nothing to diagnose"))
        lines.append((False, f"verdict: {report.verdict} — {report.advice}"))
        return lines

    for probe in (report.status, report.doctor):
        if probe is not None and not probe.ok:
            lines.append((False, f"{probe.subcmd:<8} probe failed: {probe.error}"))

    if report.doctor is not None and report.doctor.ok:
        for c in _checks(report.doctor.data):
            status = str(c.get("status", "?"))
            body = str(c.get("detail", ""))
            remedy = str(c.get("remedy", "") or "")
            if remedy:
                body += f" — remedy: {remedy}"
            lines.append((status != "FAIL",
                          f"{status:<4} {c.get('name', '?'):<28} {body}"))

    sdata = report.status.data if (report.status and report.status.ok) else None
    if sdata is not None:
        if _is_running(sdata):
            tun = sdata.get("tunnel") or {}
            state = tun.get("state", "?")
            lines.append((state == "up",
                          f"tunnel   {state} · {tun.get('endpoint', '?')} · "
                          f"since {tun.get('connected_since', '?')} · "
                          f"reconnects {tun.get('reconnects', '?')} · "
                          f"{_human_bytes(tun.get('bytes_out'))} out / "
                          f"{_human_bytes(tun.get('bytes_in'))} in · "
                          f"conns {tun.get('active_conns', '?')} active / "
                          f"{tun.get('failed_conns', '?')} failed"))
            iso = sdata.get("isolation") or {}
            if iso:
                on = bool(iso.get("enabled"))
                allow = ", ".join(iso.get("allow") or []) or "<none>"
                lines.append((True,
                              f"isolation {'enabled' if on else 'off'} · "
                              f"allow {allow} · "
                              f"{iso.get('refused', 0)} refused"))
        else:
            lines.append((False, "tunnel   planeproxy is not running"))

    lines.append((report.verdict == PP_OK,
                  f"verdict: {report.verdict} — {report.advice}"))
    return lines


# --- chat tool ---------------------------------------------------------------

_PLANEPROXY_DESC = (
    "Diagnose planeproxy, the user's SSH-tunnel tool for hostile networks "
    "(in-flight Wi-Fi, captive portals, intercepting middleboxes). Runs its "
    "read-only `status --json` and `doctor --json` probes and returns a "
    "classified verdict with the one fix that matters. USE THIS instead of "
    "ssh -v / ps / lsof / reading logs when the user's tunnel or proxy is "
    "misbehaving; it is bounded (15s) and cannot hang. It NEVER starts or "
    "stops the tunnel — up/down stay with the user. If the verdict is "
    "host-key-mismatch, treat the network as intercepting: never suggest "
    "installing a network's CA certificate or disabling TLS verification."
)

_PLANEPROXY_PARAMS = {
    "type": "object",
    "properties": {
        "check": {
            "type": "string",
            "enum": ["status", "doctor", "both"],
            "description": "Which probe to run (default: both). `doctor` "
                           "includes network reachability + host-key checks; "
                           "`status` is instant tunnel/isolation state.",
        },
    },
    "required": [],
}


def _compact_detail(report: PlaneproxyReport) -> dict:
    """Token-thrifty JSON detail for the tool result (cve_lookup style:
    prune prose the model doesn't need — PASS details, refused-host lists)."""
    out: dict = {"installed": report.installed}
    if not report.installed:
        return out
    sdata = report.status.data if (report.status and report.status.ok) else None
    if report.status is not None and not report.status.ok:
        out["status_error"] = report.status.error
    if sdata is not None:
        if not _is_running(sdata):
            out["status"] = {"running": False}
        else:
            tun = sdata.get("tunnel") or {}
            iso = sdata.get("isolation") or {}
            out["status"] = {
                "modes": sdata.get("modes"),
                "socks_addr": sdata.get("socks_addr"),
                "connect_addr": sdata.get("connect_addr"),
                "isolation": {"enabled": iso.get("enabled"),
                              "allow": iso.get("allow"),
                              "refused": iso.get("refused")},
                "tunnel": {k: tun.get(k) for k in
                           ("state", "endpoint", "connected_since",
                            "reconnects", "active_conns", "failed_conns",
                            "last_error", "fatal") if k in tun},
            }
    if report.doctor is not None and not report.doctor.ok:
        out["doctor_error"] = report.doctor.error
    ddata = report.doctor.data if (report.doctor and report.doctor.ok) else None
    if ddata is not None:
        checks = []
        for c in _checks(ddata):
            status = c.get("status")
            entry: dict = {"name": c.get("name"), "status": status}
            if status != "PASS":  # PASS details are noise; failures are signal
                entry["detail"] = c.get("detail", "")
                if c.get("remedy"):
                    entry["remedy"] = c.get("remedy")
            checks.append(entry)
        out["doctor"] = {"checks": checks,
                         "already_up": ddata.get("already_up")}
    return out


def make_planeproxy_tool():
    """(ToolDef, ToolFn) pair for the chat extra-tool seam (same pattern as
    net_probe/update_ledger). Read-only, bounded, never raises — safe in
    every session mode including no-project and read-only."""
    from luxe.tools.base import ToolDef

    def _fn(args: dict) -> tuple[str, str | None]:
        try:
            check = str((args or {}).get("check") or "both").strip().lower()
            report = full_report(check=check)
            lines = [f"verdict: {report.verdict}", report.advice,
                     json.dumps(_compact_detail(report), indent=1)]
            return "\n".join(lines), None
        except Exception as e:  # a diagnostic must never crash the loop
            return "", f"{type(e).__name__}: {e}"

    defn = ToolDef(name="planeproxy_diag", description=_PLANEPROXY_DESC,
                   parameters=_PLANEPROXY_PARAMS)
    return defn, _fn
