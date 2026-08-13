"""Claude Code diagnostics — bounded, read-only probes of the user's OTHER agent.

luxe is the fleet's local fallback dev tool, so the question "what is wrong with
Claude Code right now?" lands here by construction — and on 2026-08-13 it landed
badly: a session had silently reverted from the Max-plan login to the Platform
API key, and a chat with no instrument could only guess. The evidence was all
sitting on disk and in the process table; nothing could read it.

This module is that instrument. It answers ONE question well — **which billing
path is Claude Code actually on, and what is overriding it** — plus the
surrounding config/process/session state, deterministically and with no model in
the loop.

Three surfaces share it (mirroring netdiag.py / planeproxy.py):
  - `claude_code_diag` tool (chat-only, via the extra-tool seam) — one bounded
    call instead of improvised `ps`/`cat ~/.claude/...` forensics;
  - `/claude [status|net|all]` in chat — deterministic report;
  - `luxe claudecode` CLI — same report + exit code for scripts.

Design rules:
  - Bounded: every subprocess carries `_TIMEOUT_S`; every file read is capped.
    A hang or a huge transcript becomes a note, never a stuck surface.
  - Never raises: missing binary, unparseable JSON, permission errors — all are
    data in the report. Absence is a structured verdict, NOT an error string
    (tools/analysis.py B3: agents read errors as false signals).
  - READ-ONLY. It never launches, kills, or reconfigures Claude Code, and never
    writes under `~/.claude`. Switching billing path stays with the user
    (`claude-plan` / `claude-api`).
  - **NO SECRET VALUES, EVER.** Environment variables are reported by NAME and
    presence only — the `ps eww` line this parses carries the values of every
    other variable in the process, and the `env:` block in settings.json can
    literally hold an API key. Keychain lookups are metadata-only (`security
    find-generic-password` WITHOUT `-w`), so nothing is ever decrypted. The
    approved/rejected key fragments in ~/.claude.json are counted, never shown.
  - **NO CONVERSATION CONTENT.** Session transcripts are read for METADATA only
    (timestamp, model, effort, cwd, gitBranch, CLI version, record counts).
    `message.content` is never touched, and neither is `~/.claude/CLAUDE.md`.
    This is the sanctioned boundary for the `~/.claude` prohibition, which is
    scoped to context/memory (memory.sdd) — the same carve-out shape as
    `chat/theme.py` reading the statusline theme name.
  - Pure logic + dataclasses here; rich rendering stays in the surfaces
    (cmd_diag.py / cli.py). `classify()` is pure and unit-tested against canned
    facts (tests/test_claudecode.py).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# One deadline per subprocess. `claude --version` on a cold native install is
# the slow one; 10s is generous slack and a breach is itself a finding.
_TIMEOUT_S = 10.0

# Transcripts are append-only and can reach tens of MB. Metadata lives in every
# record, so the TAIL is representative and the head only supplies a start time.
_TAIL_BYTES = 256 * 1024
_MAX_SESSIONS = 5

# Environment variables that decide (or redirect) the billing path. Reported by
# NAME and presence only — never a value. Order is the display order.
AUTH_ENV_NAMES: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)

# A key or token in the environment BILLS the Platform API instead of the
# subscription. A base-url/bedrock/vertex setting redirects the traffic itself.
_KEY_ENV_NAMES = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"})
_GATEWAY_ENV_NAMES = frozenset({"ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                                "CLAUDE_CODE_USE_VERTEX"})

# `claude <sub>` forms that are plumbing, not an interactive session.
_HELPER_SUBCOMMANDS = frozenset({"daemon", "bg-pty-host", "bg-spare", "mcp",
                                 "install", "update", "doctor", "migrate-installer"})

# The macOS Keychain service Claude Code stores its OAuth credentials under, and
# the one `claude-api` (dotfiles zsh/claude.zsh) stores the Platform key under.
_OAUTH_KEYCHAIN_SERVICE = "Claude Code-credentials"
_API_KEY_KEYCHAIN_SERVICE = "ANTHROPIC_API_KEY"
_API_KEY_EXPIRY_KEYCHAIN_SERVICE = "ANTHROPIC_API_KEY_EXPIRY"

ANTHROPIC_API_HOST = "api.anthropic.com"


# --- verdicts ----------------------------------------------------------------

CC_NOT_INSTALLED = "not-installed"
CC_SETTINGS_INVALID = "settings-invalid"
CC_GATEWAY_OVERRIDE = "gateway-override"
CC_API_KEY_SESSION = "api-key-session"
CC_UNREACHABLE = "api-unreachable"
CC_NO_AUTH = "no-auth"
CC_API_KEY_AMBIENT = "api-key-ambient"
CC_OK = "ok"

_ADVICE = {
    CC_NOT_INSTALLED: (
        "the `claude` CLI is not on PATH or at ~/.local/bin/claude — nothing "
        "to diagnose on this host."
    ),
    CC_SETTINGS_INVALID: (
        "a settings file does not parse, so Claude Code is ignoring it (or "
        "refusing to start). Fix the JSON at the path marked FAIL — the parse "
        "error names the line."
    ),
    CC_GATEWAY_OVERRIDE: (
        "traffic is being REDIRECTED away from the normal endpoint "
        "(ANTHROPIC_BASE_URL / Bedrock / Vertex). That is deliberate for a "
        "gateway or proxy setup and wrong for everything else — clear the "
        "variable, or the `env:` block in settings.json that sets it, to go "
        "back to the direct endpoint."
    ),
    CC_API_KEY_SESSION: (
        "a RUNNING Claude Code session has an API key/token in its "
        "environment, so it is billing the Platform API — not the Max "
        "subscription. That session will not change path while it lives: quit "
        "it and relaunch with `claude-plan` (which strips ANTHROPIC_API_KEY). "
        "Use `claude-api` only when the API path is what you want."
    ),
    CC_UNREACHABLE: (
        f"{ANTHROPIC_API_HOST} is not reachable from this host — this is a "
        "network problem, not a Claude Code problem. Run `/net` (or `luxe "
        "net`) for the layered verdict, and `/planeproxy` if you are on a "
        "hostile network."
    ),
    CC_NO_AUTH: (
        "no credential is resolvable: no OAuth login in the Keychain and no "
        "API key anywhere. Run `claude` and log in, or seed a Platform key "
        "with `claude-api --new-key`."
    ),
    CC_API_KEY_AMBIENT: (
        "no running session is on the API path, but an API key IS reachable "
        "from the ambient environment or a settings `env:` block — so the "
        "NEXT plain `claude` you launch may bill the Platform API instead of "
        "the subscription. Launch with `claude-plan` to force the "
        "subscription, or unset the variable."
    ),
    CC_OK: (
        "Claude Code is installed, authenticated on the subscription login, "
        "and nothing is overriding the billing path."
    ),
}


# --- fact dataclasses --------------------------------------------------------


@dataclass
class Install:
    present: bool
    bin_path: str = ""
    version: str = ""
    install_method: str = ""
    auto_updates: bool | None = None
    startups: int = 0


@dataclass
class Auth:
    """Everything that decides which credential a NEW session would pick up.

    Every field is presence/metadata. No value here is ever a secret.
    """
    oauth_keychain: bool = False
    oauth_keychain_modified: str = ""     # from the Keychain `mdat` attribute
    oauth_creds_file: bool = False        # ~/.claude/.credentials.json fallback
    account_email: str = ""
    billing_type: str = ""
    seat_tier: str = ""
    api_key_keychain: bool = False        # claude-api's store, existence only
    api_key_expiry: str = ""              # ISO date, if the user recorded one
    ambient_env: list[str] = field(default_factory=list)   # NAMES in luxe's env
    settings_env: list[str] = field(default_factory=list)  # NAMES in settings
    api_key_helper: bool = False
    approved_keys: int = 0
    rejected_keys: int = 0


@dataclass
class Proc:
    pid: int
    etime: str
    argv: str
    kind: str                             # "session" | "helper"
    auth_env: list[str] = field(default_factory=list)   # NAMES only
    env_readable: bool = True             # False = ps gave us no environment


@dataclass
class SettingsFile:
    path: str
    exists: bool
    valid: bool = True
    error: str = ""
    model: str = ""
    env_keys: list[str] = field(default_factory=list)
    hooks: int = 0
    permission_mode: str = ""
    api_key_helper: bool = False


@dataclass
class SessionInfo:
    """Metadata-only summary of one Claude Code transcript. No message text."""
    session_id: str
    cwd: str = ""
    branch: str = ""
    started: str = ""
    ended: str = ""
    assistant_turns: int = 0
    models: list[str] = field(default_factory=list)
    efforts: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    truncated: bool = False               # only the tail was read


@dataclass
class Facts:
    """The complete probe result. `classify()` consumes exactly this — pure."""
    install: Install
    auth: Auth
    settings: list[SettingsFile] = field(default_factory=list)
    procs: list[Proc] = field(default_factory=list)
    sessions: list[SessionInfo] = field(default_factory=list)
    net_verdict: str = ""                 # "" = not probed
    errors: list[str] = field(default_factory=list)


@dataclass
class ClaudeCodeReport:
    facts: Facts
    net: object | None = None             # netdiag.LadderReport when probed
    verdict: str = CC_OK
    advice: str = ""


# --- small bounded helpers ---------------------------------------------------


def _run(argv: list[str]) -> tuple[str, str, int]:
    """Bounded subprocess. Returns (stdout, stderr, rc); rc=-1 on timeout/OSError."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return "", "", -1
    return proc.stdout or "", proc.stderr or "", proc.returncode


def _read_json(path: Path) -> tuple[dict | None, str]:
    """Parse a JSON file. Returns (data, error); a missing file is (None, "")."""
    try:
        raw = path.read_text(errors="replace")
    except FileNotFoundError:
        return None, ""
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON at line {e.lineno} col {e.colno}: {e.msg}"
    return (data if isinstance(data, dict) else None,
            "" if isinstance(data, dict) else "top level is not an object")


def resolve_bin() -> str | None:
    """The `claude` binary: PATH first, then the native install spot."""
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    return str(fallback) if fallback.is_file() else None


# --- keychain (metadata only — never `-w`, so nothing is ever decrypted) -----

_MDAT_RE = re.compile(r'"mdat"<timedate>=0x[0-9A-F]*\s+"(\d{14})Z')


def _keychain_entry(service: str) -> tuple[bool, str]:
    """(exists, last_modified_iso) for a generic-password entry.

    Deliberately WITHOUT `-w`: this asks only whether the entry exists and when
    it was last written. Reading the secret would prompt for Keychain access and
    put a credential in a diagnostic that gets written to a transcript.
    """
    out, err, rc = _run(["security", "find-generic-password", "-s", service])
    if rc != 0:
        return False, ""
    m = _MDAT_RE.search(out + err)
    if not m:
        return True, ""
    d = m.group(1)
    return True, f"{d[0:4]}-{d[4:6]}-{d[6:8]} {d[8:10]}:{d[10:12]}Z"


# --- process table -----------------------------------------------------------


def _classify_argv(argv: str) -> str | None:
    """"session" | "helper" for a `claude …` command line; None if not claude."""
    parts = argv.split()
    if not parts:
        return None
    if os.path.basename(parts[0]) != "claude":
        return None
    sub = parts[1] if len(parts) > 1 else ""
    return "helper" if sub in _HELPER_SUBCOMMANDS else "session"


def _proc_auth_env(pid: int) -> tuple[list[str], bool]:
    """The AUTH_ENV_NAMES set in pid's environment, by NAME. (names, readable).

    `ps eww` prints the whole environment — including every OTHER variable's
    VALUE. This function must therefore never return, log, or store anything
    but the matched names. `readable=False` means ps showed us no environment
    at all (another user's process, or a hardened binary), which is different
    from "no auth variables set" and must not be reported as a clean bill.
    """
    out, _, rc = _run(["ps", "eww", "-p", str(pid), "-o", "command="])
    if rc != 0 or not out.strip():
        return [], False
    tokens = out.split()
    # Any NAME=… token means ps really did expose the environment.
    readable = any("=" in t and t.split("=", 1)[0].isupper() for t in tokens)
    names = {t.split("=", 1)[0] for t in tokens if "=" in t}
    return [n for n in AUTH_ENV_NAMES if n in names], readable


def _probe_procs() -> tuple[list[Proc], list[str]]:
    out, _, rc = _run(["ps", "-axo", "pid=,etime=,command="])
    if rc != 0:
        return [], ["process table unreadable (ps failed or timed out)"]
    procs: list[Proc] = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, etime, argv = parts
        kind = _classify_argv(argv)
        if kind is None:
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        auth_env, readable = ([], True) if kind == "helper" else _proc_auth_env(pid)
        procs.append(Proc(pid=pid, etime=etime, argv=argv[:160], kind=kind,
                          auth_env=auth_env, env_readable=readable))
    return procs, []


# --- settings ----------------------------------------------------------------


def _settings_paths(repo_path: str | None) -> list[Path]:
    home = Path.home()
    paths = [home / ".claude" / "settings.json",
             home / ".claude" / "settings.local.json"]
    if repo_path:
        root = Path(repo_path)
        paths += [root / ".claude" / "settings.json",
                  root / ".claude" / "settings.local.json"]
    return paths


def _probe_settings(repo_path: str | None) -> list[SettingsFile]:
    files: list[SettingsFile] = []
    for path in _settings_paths(repo_path):
        if not path.exists():
            files.append(SettingsFile(path=str(path), exists=False))
            continue
        data, err = _read_json(path)
        if data is None:
            files.append(SettingsFile(path=str(path), exists=True, valid=False,
                                      error=err or "unreadable"))
            continue
        env_block = data.get("env") if isinstance(data.get("env"), dict) else {}
        hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
        perms = (data.get("permissions")
                 if isinstance(data.get("permissions"), dict) else {})
        files.append(SettingsFile(
            path=str(path), exists=True, valid=True,
            model=str(data.get("model") or ""),
            # KEYS only — an `env:` block is a documented place to put an API key.
            env_keys=sorted(str(k) for k in env_block),
            hooks=sum(len(v) if isinstance(v, list) else 1 for v in hooks.values()),
            permission_mode=str(perms.get("defaultMode") or ""),
            api_key_helper=bool(data.get("apiKeyHelper")),
        ))
    return files


# --- ~/.claude.json + credentials -------------------------------------------


def _probe_auth(settings: list[SettingsFile]) -> tuple[Auth, Install, list[str]]:
    errors: list[str] = []
    home = Path.home()

    bin_path = resolve_bin()
    install = Install(present=bin_path is not None, bin_path=bin_path or "")
    if bin_path:
        out, _, rc = _run([bin_path, "--version"])
        install.version = out.strip().splitlines()[0] if (rc == 0 and out.strip()) else ""

    auth = Auth()
    data, err = _read_json(home / ".claude.json")
    if err:
        errors.append(f"~/.claude.json: {err}")
    if data:
        install.install_method = str(data.get("installMethod") or "")
        au = data.get("autoUpdates")
        install.auto_updates = au if isinstance(au, bool) else None
        install.startups = int(data.get("numStartups") or 0)
        acct = data.get("oauthAccount")
        if isinstance(acct, dict):
            auth.account_email = str(acct.get("emailAddress") or "")
            auth.billing_type = str(acct.get("billingType") or "")
            auth.seat_tier = str(acct.get("seatTier") or "")
        resp = data.get("customApiKeyResponses")
        if isinstance(resp, dict):
            # COUNTS only. The lists hold key fragments — never render them.
            for key, attr in (("approved", "approved_keys"),
                              ("rejected", "rejected_keys")):
                val = resp.get(key)
                setattr(auth, attr, len(val) if isinstance(val, list) else 0)

    auth.oauth_keychain, auth.oauth_keychain_modified = _keychain_entry(
        _OAUTH_KEYCHAIN_SERVICE)
    auth.oauth_creds_file = (home / ".claude" / ".credentials.json").is_file()
    auth.api_key_keychain, _ = _keychain_entry(_API_KEY_KEYCHAIN_SERVICE)
    # The expiry is a DATE, not a secret, and claude-api records it alongside
    # the key; `-w` is safe here and nowhere else in this module.
    if _keychain_entry(_API_KEY_EXPIRY_KEYCHAIN_SERVICE)[0]:
        out, _, rc = _run(["security", "find-generic-password", "-s",
                           _API_KEY_EXPIRY_KEYCHAIN_SERVICE, "-w"])
        cand = out.strip()
        if rc == 0 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cand):
            auth.api_key_expiry = cand

    # luxe's own process environment stands in for "the ambient shell": luxe was
    # launched from one, so what it inherited is what a sibling `claude` would.
    auth.ambient_env = [n for n in AUTH_ENV_NAMES if os.environ.get(n)]
    seen: list[str] = []
    for sf in settings:
        auth.api_key_helper = auth.api_key_helper or sf.api_key_helper
        seen += [k for k in sf.env_keys if k in AUTH_ENV_NAMES and k not in seen]
    auth.settings_env = seen
    return auth, install, errors


# --- session transcripts (METADATA ONLY) -------------------------------------


def _tail_lines(path: Path, max_bytes: int = _TAIL_BYTES) -> tuple[list[str], bool]:
    """Last <=max_bytes of a file as whole lines. (lines, truncated).

    Transcripts are append-only JSONL and can reach tens of MB; every record
    carries the metadata this module wants, so the tail is representative and
    bounded. A partial first line is dropped when we truncated.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                truncated = True
            else:
                truncated = False
            blob = fh.read()
    except OSError:
        return [], False
    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if truncated and lines:
        lines = lines[1:]
    return lines, truncated


def _probe_sessions(limit: int = _MAX_SESSIONS) -> tuple[list[SessionInfo], list[str]]:
    """Summarize the most recently written transcripts.

    Reads ONLY metadata fields. `message.content` is never accessed — the point
    is what the session was CONFIGURED as (model, effort, CLI version, cwd),
    not what was said in it.
    """
    root = Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return [], []
    try:
        files = [p for d in root.iterdir() if d.is_dir()
                 for p in d.glob("*.jsonl")]
    except OSError as e:
        return [], [f"~/.claude/projects unreadable: {type(e).__name__}: {e}"]
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        pass

    out: list[SessionInfo] = []
    for path in files[:limit]:
        lines, truncated = _tail_lines(path)
        info = SessionInfo(session_id=path.stem, truncated=truncated)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            ts = str(rec.get("timestamp") or "")
            if ts:
                info.started = info.started or ts
                info.ended = ts
            for key, attr in (("cwd", "cwd"), ("gitBranch", "branch")):
                val = rec.get(key)
                if val and not getattr(info, attr):
                    setattr(info, attr, str(val))
            for key, bucket in (("version", info.versions),
                                ("effort", info.efforts)):
                val = rec.get(key)
                if val and str(val) not in bucket:
                    bucket.append(str(val))
            if rec.get("type") == "assistant":
                info.assistant_turns += 1
                msg = rec.get("message")
                model = msg.get("model") if isinstance(msg, dict) else None
                # `<synthetic>` is Claude Code's stand-in on locally generated
                # assistant records (errors, interrupts). Counting it as a model
                # made every session with one interrupt read "CHANGED
                # mid-session" — a false positive on the one signal that has to
                # stay trustworthy.
                if model and str(model) != "<synthetic>" \
                        and str(model) not in info.models:
                    info.models.append(str(model))
        out.append(info)
    return out, []


# --- classification (PURE) ---------------------------------------------------


def _live_sessions(facts: Facts) -> list[Proc]:
    return [p for p in facts.procs if p.kind == "session"]


def classify(facts: Facts) -> str:
    """Decision tree over collected facts, most-broken verdict first. PURE — no
    I/O, unit-tested against canned Facts.

    Precedence rationale: nothing to diagnose (not-installed) outranks
    everything; a settings file that does not parse is silently ignored by
    Claude Code and explains arbitrary downstream weirdness, so it comes next;
    a redirect of the endpoint itself outranks a mere credential swap; a LIVE
    session on the API key is the concrete, currently-costing-money finding and
    outranks unreachability (which `/net` diagnoses better anyway); no
    credential at all outranks a merely ambient one.
    """
    if not facts.install.present:
        return CC_NOT_INSTALLED
    if any(sf.exists and not sf.valid for sf in facts.settings):
        return CC_SETTINGS_INVALID

    gateway = set(facts.auth.ambient_env) | set(facts.auth.settings_env)
    for proc in _live_sessions(facts):
        gateway |= set(proc.auth_env)
    if gateway & _GATEWAY_ENV_NAMES:
        return CC_GATEWAY_OVERRIDE

    if any(set(p.auth_env) & _KEY_ENV_NAMES for p in _live_sessions(facts)):
        return CC_API_KEY_SESSION

    if facts.net_verdict and facts.net_verdict not in ("ok", "degraded"):
        return CC_UNREACHABLE

    has_key = bool((set(facts.auth.ambient_env) | set(facts.auth.settings_env))
                   & _KEY_ENV_NAMES) or facts.auth.api_key_keychain
    has_oauth = facts.auth.oauth_keychain or facts.auth.oauth_creds_file
    if not has_oauth and not has_key and not facts.auth.api_key_helper:
        return CC_NO_AUTH

    # A key sitting in the Keychain is claude-api's *store* and is inert until
    # that wrapper injects it — only an ambient/settings key changes what a
    # plain `claude` would pick up.
    if (set(facts.auth.ambient_env) | set(facts.auth.settings_env)) & _KEY_ENV_NAMES:
        return CC_API_KEY_AMBIENT

    return CC_OK


# --- full report -------------------------------------------------------------


def full_report(check: str = "all", repo_path: str | None = None) -> ClaudeCodeReport:
    """Run the requested probes and classify. `check` is status|net|all.

    Never raises. `status` is local-only (no network at all); `net` adds the
    bounded DNS→TCP→TLS→HTTP ladder against api.anthropic.com.
    """
    check = check if check in ("status", "net", "all") else "all"

    settings = _probe_settings(repo_path)
    auth, install, errors = _probe_auth(settings)
    facts = Facts(install=install, auth=auth, settings=settings, errors=errors)

    if install.present:
        facts.procs, proc_errors = _probe_procs()
        facts.errors += proc_errors
        facts.sessions, sess_errors = _probe_sessions()
        facts.errors += sess_errors

    ladder = None
    if check in ("net", "all"):
        try:
            from luxe import netdiag
            ladder = netdiag.run_ladder(ANTHROPIC_API_HOST)
            facts.net_verdict = ladder.verdict
        except Exception as e:            # a probe failure is data, not a crash
            facts.errors.append(f"api.anthropic.com ladder failed: "
                                f"{type(e).__name__}: {e}")

    report = ClaudeCodeReport(facts=facts, net=ladder)
    report.verdict = classify(facts)
    report.advice = _ADVICE[report.verdict]
    return report


# --- rendering (pure text; surfaces apply glyphs/colour) ---------------------


def _fmt(label: str, body: str) -> str:
    return f"{label:<10} {body}"


def render_lines(report: ClaudeCodeReport) -> list[tuple[bool, str]]:
    """(ok, text) pairs for the surfaces to glyph/colour — pure text here, rich
    markup belongs to the renderer (cmd_diag.py / cli.py)."""
    f = report.facts
    lines: list[tuple[bool, str]] = []

    if not f.install.present:
        lines.append((False, _fmt("install", "claude not found on PATH or "
                                             "~/.local/bin — nothing to diagnose")))
        lines.append((False, f"verdict: {report.verdict} — {report.advice}"))
        return lines

    ver = f.install.version or "(version unknown)"
    extra = " · ".join(x for x in (
        f.install.install_method,
        "auto-update off" if f.install.auto_updates is False else "",
    ) if x)
    lines.append((True, _fmt("install", f"{ver} · {f.install.bin_path}"
                                        + (f" · {extra}" if extra else ""))))

    a = f.auth
    if a.oauth_keychain or a.oauth_creds_file:
        where = "Keychain" if a.oauth_keychain else "~/.claude/.credentials.json"
        when = f" · refreshed {a.oauth_keychain_modified}" if a.oauth_keychain_modified else ""
        lines.append((True, _fmt("login", f"OAuth credentials present ({where})"
                                          f"{when}")))
    else:
        lines.append((False, _fmt("login", "no OAuth credentials found")))

    if a.account_email or a.billing_type or a.seat_tier:
        desc = " · ".join(x for x in (a.account_email, a.billing_type,
                                      a.seat_tier) if x)
        lines.append((True, _fmt("account", desc)))

    # --- the headline: which path is each live session actually on -----------
    sessions = _live_sessions(f)
    helpers = [p for p in f.procs if p.kind == "helper"]
    if not sessions:
        lines.append((True, _fmt("sessions", "no interactive `claude` process "
                                             "running")))
    for proc in sessions:
        if not proc.env_readable:
            lines.append((False, _fmt("session", f"pid {proc.pid} · up {proc.etime} "
                                                 "· environment not readable — "
                                                 "billing path UNKNOWN")))
            continue
        keys = [n for n in proc.auth_env if n in _KEY_ENV_NAMES]
        gate = [n for n in proc.auth_env if n in _GATEWAY_ENV_NAMES]
        if keys or gate:
            what = " + ".join(keys + gate)
            path = "Platform API key" if keys else "redirected endpoint"
            lines.append((False, _fmt("session", f"pid {proc.pid} · up {proc.etime} "
                                                 f"· {path} ({what} set)")))
        else:
            lines.append((True, _fmt("session", f"pid {proc.pid} · up {proc.etime} "
                                                "· subscription login (no API "
                                                "key in its environment)")))
    if helpers:
        lines.append((True, _fmt("helpers", f"{len(helpers)} background "
                                            f"process(es): "
                                            + ", ".join(sorted(
                                                {p.argv.split()[1] for p in helpers
                                                 if len(p.argv.split()) > 1})))))

    # --- what would affect the NEXT launch -----------------------------------
    if a.ambient_env:
        lines.append((False, _fmt("ambient", "set in the shell luxe inherited: "
                                             + ", ".join(a.ambient_env))))
    else:
        lines.append((True, _fmt("ambient", "no ANTHROPIC_*/CLAUDE_CODE_* "
                                            "overrides in luxe's environment")))
    if a.settings_env:
        lines.append((False, _fmt("settings", "env block sets: "
                                              + ", ".join(a.settings_env))))
    if a.api_key_helper:
        lines.append((False, _fmt("settings", "apiKeyHelper is configured — it "
                                              "supplies a key on every launch")))
    if a.api_key_keychain:
        exp = f" · expires {a.api_key_expiry}" if a.api_key_expiry else ""
        lines.append((True, _fmt("keychain", f"a Platform key is stored for "
                                             f"`claude-api`{exp} (inert until "
                                             "that wrapper injects it)")))
    if a.approved_keys or a.rejected_keys:
        lines.append((True, _fmt("prompts", f"{a.approved_keys} API key(s) "
                                            f"approved, {a.rejected_keys} "
                                            "rejected in this install")))

    for sf in f.settings:
        if not sf.exists:
            continue
        if not sf.valid:
            lines.append((False, _fmt("config", f"{sf.path}: {sf.error}")))
            continue
        bits = [x for x in (f"model={sf.model}" if sf.model else "",
                            f"mode={sf.permission_mode}" if sf.permission_mode else "",
                            f"{sf.hooks} hook(s)" if sf.hooks else "") if x]
        if bits:
            lines.append((True, _fmt("config", f"{sf.path}: " + " · ".join(bits))))

    # --- recent sessions: metadata only --------------------------------------
    for s in f.sessions:
        if not s.assistant_turns and not s.models:
            continue
        drift = len(s.versions) > 1 or len(s.models) > 1
        detail = (f"{s.session_id[:8]} · {s.cwd or '?'} · {s.assistant_turns} "
                  f"replies · model {', '.join(s.models) or '?'}"
                  + (f" · effort {', '.join(s.efforts)}" if s.efforts else "")
                  + (f" · cli {', '.join(s.versions)}" if s.versions else "")
                  + (" · CHANGED mid-session" if drift else ""))
        lines.append((not drift, _fmt("recent", detail)))

    if report.net is not None:
        # The ladder carries its own targets (it also probes a captive portal
        # and a DNS-free IP), so label each rung with the target it actually hit
        # rather than assuming api.anthropic.com.
        for probe in getattr(report.net, "probes", []):
            lines.append((probe.ok, _fmt("net", f"{probe.layer:<6} "
                                                f"{probe.target} — "
                                                f"{probe.detail or probe.error}")))

    for err in f.errors:
        lines.append((False, _fmt("note", err)))

    lines.append((report.verdict == CC_OK,
                  f"verdict: {report.verdict} — {report.advice}"))
    return lines


# --- chat tool ---------------------------------------------------------------

_CC_DESC = (
    "Diagnose the user's OTHER agent, Claude Code (the `claude` CLI), when it "
    "misbehaves: which billing path a running session is actually on (Max "
    "subscription login vs Platform API key), whether anything is redirecting "
    "the endpoint (ANTHROPIC_BASE_URL / Bedrock / Vertex), whether settings "
    "files parse, what the recent sessions were configured with, and whether "
    "api.anthropic.com is reachable. USE THIS instead of running ps / cat "
    "~/.claude/... / env yourself — it is bounded, read-only, and it reports "
    "environment variables by NAME ONLY so no secret is ever exposed. It never "
    "starts, stops, or reconfigures Claude Code. If the verdict is "
    "api-key-session, tell the user the running session must be relaunched "
    "with `claude-plan` — a live session cannot change billing path."
)

_CC_PARAMS = {
    "type": "object",
    "properties": {
        "check": {
            "type": "string",
            "enum": ["status", "net", "all"],
            "description": "Which probes to run (default: all). `status` is "
                           "local-only and instant; `net` adds the bounded "
                           "DNS/TCP/TLS/HTTP ladder to api.anthropic.com.",
        },
    },
    "required": [],
}


def _compact_detail(report: ClaudeCodeReport) -> dict:
    """Token-thrifty JSON for the tool result: the decision-relevant facts, not
    the full dump (planeproxy `_compact_detail` convention)."""
    f = report.facts
    a = f.auth
    out: dict = {
        "installed": f.install.present,
        "version": f.install.version,
        "login": {
            "oauth_credentials": a.oauth_keychain or a.oauth_creds_file,
            "account": a.account_email,
            "billing_type": a.billing_type,
            "seat_tier": a.seat_tier,
        },
        # NAMES ONLY — never values.
        "ambient_env": a.ambient_env,
        "settings_env": a.settings_env,
        "api_key_helper": a.api_key_helper,
        "api_key_in_keychain": a.api_key_keychain,
        "live_sessions": [
            {"pid": p.pid, "up": p.etime, "auth_env": p.auth_env,
             "env_readable": p.env_readable}
            for p in _live_sessions(f)
        ],
        "settings_invalid": [sf.path for sf in f.settings
                             if sf.exists and not sf.valid],
        "recent_sessions": [
            {"id": s.session_id[:8], "cwd": s.cwd, "replies": s.assistant_turns,
             "models": s.models, "efforts": s.efforts, "cli_versions": s.versions}
            for s in f.sessions if s.assistant_turns
        ],
    }
    if f.net_verdict:
        out["api_reachability"] = f.net_verdict
    if f.errors:
        out["notes"] = f.errors
    return out


def make_claude_code_tool():
    """(ToolDef, ToolFn) pair for the chat extra-tool seam (same pattern as
    net_probe / planeproxy_diag). Read-only, bounded, never raises — safe in
    every session mode including no-project and read-only."""
    from luxe.tools.base import ToolDef

    def _fn(args: dict) -> tuple[str, str | None]:
        try:
            check = str((args or {}).get("check") or "all").strip().lower()
            report = full_report(check=check)
            body = [f"verdict: {report.verdict}", report.advice,
                    json.dumps(_compact_detail(report), indent=1)]
            return "\n".join(body), None
        except Exception as e:  # a diagnostic must never crash the loop
            return "", f"{type(e).__name__}: {e}"

    defn = ToolDef(name="claude_code_diag", description=_CC_DESC,
                   parameters=_CC_PARAMS)
    return defn, _fn
