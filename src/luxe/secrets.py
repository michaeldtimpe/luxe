"""Resolve oMLX API keys without depending on shell hygiene.

Order: process env → `~/.luxe/secrets.env` (KEY=VALUE lines; an `export `
prefix is tolerated) → macOS login keychain (service name == the env-var
name). First non-empty wins; every step is guarded and returns "" rather
than raising.

Motivation (2026-07-30): secrets.env has no `export` lines, so any shell
that `source`s it without `set -a` launches luxe with an empty environment
and every request 401s. The m1 masked this for months (its shell profile
exports the key); the m5 surfaced it inside `luxe smoke --code` — the exact
outage-drill scenario the fallback kit exists for. A fallback tool must not
outsource its own credentials to per-host dotfile discipline.

Keys still NEVER live in repo YAML (luxe.sdd); this reads only the
sanctioned local locations CLAUDE.md already names.

Since 2026-10-22 this module is also the ONE place that decides what counts
as a secret, for two guards (tools.sdd, agents.sdd):

- `is_secret_name` / `scrubbed_env` — the environment every MODEL-DRIVEN
  subprocess gets (bash, analyzers, test runners, gitchange verify): every
  secret-named variable dropped.
- `known_secret_values` / `redact` / `secret_in_args` — the VALUES, so the
  agent loop can redact them out of tool results and refuse a tool call whose
  arguments carry one.

Threat model: a model (backdoored fine-tune, poisoned chat template, or a
prompt injection carried in by a file or web page) steers the agent into
reading credentials and sending them somewhere. Sandboxing is out of scope.
"""

from __future__ import annotations

from luxe.paths import luxe_home

import base64
import os
import re
import subprocess
import urllib.parse
from collections.abc import Mapping
from typing import Any

SECRETS_PATH = luxe_home() / "secrets.env"


def _from_file(name: str) -> str:
    try:
        text = SECRETS_PATH.read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


# Keychain answers, per process. Every chat-side Backend construction resolves
# its key, and a miss in env + secrets.env fell through to a `security`
# subprocess EVERY time (≤3s each) — `/doctor`, a `/backend` probe, a smoke
# run each paid it again for an answer that cannot change mid-process. Misses
# are cached too: a keyless host (neo's llama-server) is exactly the one that
# paid on every call. env and secrets.env stay live (cheap, and editable).
# Only DEFINITIVE answers are cached: a found key, or `security`'s exit 44
# ("item not found"). A locked keychain, a denied prompt or a timeout is
# transient — caching it would pin "no key" for the life of the process.
_KEYCHAIN_CACHE: dict[str, str] = {}
_SECURITY_NOT_FOUND = 44


def _from_keychain(name: str) -> str:
    if name in _KEYCHAIN_CACHE:
        return _KEYCHAIN_CACHE[name]
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", name, "-w"],
            capture_output=True, text=True, timeout=3,
            # `security` can prompt; it must never read luxe's stdin.
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        return ""          # not cached: a timeout may be transient
    if r.returncode == 0:
        value = r.stdout.strip()
    elif r.returncode == _SECURITY_NOT_FOUND:
        value = ""
    else:
        return ""          # locked / denied: transient, ask again next time
    _KEYCHAIN_CACHE[name] = value
    return value


def resolve_api_key(env_name: str = "OMLX_API_KEY") -> str:
    """The API key for `env_name`, from env → secrets.env → keychain.

    The NAME is remembered (`_RESOLVED_NAMES`): whatever luxe itself uses as a
    credential is a secret name, whatever it happens to be called."""
    _RESOLVED_NAMES.add(env_name)
    return (os.environ.get(env_name, "")
            or _from_file(env_name)
            or _from_keychain(env_name))


# --- what counts as a secret (2026-10-22) ------------------------------------

#: Names luxe asked `resolve_api_key` for in this process (backend keys, web
#: provider keys, relay tokens) — secret by use, not by spelling.
_RESOLVED_NAMES: set[str] = set()

#: Substrings that make a variable NAME credential-shaped (matched on the
#: upper-cased name) …
_SECRET_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSW", "CREDENTIAL",
                      "PRIVATE", "COOKIE")
#: … and suffixes (`AZURE_DEVOPS_PAT`, `NPM_CONFIG__AUTH`). `_AUTH` is a
#: suffix rule, not a substring, so `SSH_AUTH_SOCK` stays visible.
_SECRET_SUFFIXES = ("_PAT", "_AUTH")

#: Words that CONTAIN a pattern without being one. They are removed from the
#: name before matching, so `KEYCHAIN_PATH` and `TOKENIZERS_PARALLELISM` pass
#: while `KEYCHAIN_PASSWORD` is still caught by its `PASSW`.
_BENIGN_WORDS = ("KEYCHAIN", "KEYBOARD", "KEYMAP", "KEYTIMEOUT", "TOKENIZERS")

#: Whole names that are never secrets, whatever they match: the agent socket
#: and TTY a child legitimately needs (ssh, commit signing), and Go's
#: module-privacy config (`GOPRIVATE` is a host list; Go builds need it).
_KEEP_NAMES = frozenset({"SSH_AUTH_SOCK", "GPG_TTY", "GPG_AGENT_INFO",
                         "GOPRIVATE", "GONOPRIVATE"})

#: Values shorter than this are never treated as known secrets: redacting
#: "true" or "8192" everywhere would mangle ordinary output.
_MIN_SECRET_LEN = 8

_FILE_CACHE: tuple[tuple, dict[str, str]] | None = None


def _file_items() -> dict[str, str]:
    """Every KEY=VALUE in secrets.env (same line grammar as `_from_file`),
    cached on the file's (path, mtime, size, inode) — cheap enough to ask on
    every tool call."""
    global _FILE_CACHE
    try:
        st = SECRETS_PATH.stat()
        sig: tuple = (str(SECRETS_PATH), st.st_mtime_ns, st.st_size, st.st_ino)
        if _FILE_CACHE is not None and _FILE_CACHE[0] == sig:
            return _FILE_CACHE[1]
        text = SECRETS_PATH.read_text()
    except OSError:
        return {}
    items: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if sep and key.strip():
            items[key.strip()] = value.strip().strip('"').strip("'")
    _FILE_CACHE = (sig, items)
    return items


def _pattern_match(name: str) -> bool:
    upper = name.upper()
    for word in _BENIGN_WORDS:
        upper = upper.replace(word, "_")
    return (any(s in upper for s in _SECRET_SUBSTRINGS)
            or upper.endswith(_SECRET_SUFFIXES))


def _explicit_names(file_items: Mapping[str, str] | None = None) -> set[str]:
    """Names that are secrets because of WHERE they come from, not how they
    are spelled: listed in secrets.env, resolved by luxe, held in Keychain."""
    items = _file_items() if file_items is None else file_items
    return set(items) | _RESOLVED_NAMES | set(_KEYCHAIN_CACHE)


def is_secret_name(name: str, *, _explicit: set[str] | None = None) -> bool:
    """True when the environment variable `name` must not reach a
    model-driven subprocess (and its value must not reach the model).

    Secret = listed in `~/.luxe/secrets.env`, OR a name luxe resolved as a
    credential (`resolve_api_key`, Keychain), OR credential-shaped: contains
    KEY / TOKEN / SECRET / PASSW / CREDENTIAL / PRIVATE / COOKIE, or ends in
    `_PAT` / `_AUTH`. The explicit sources win over everything; the small
    `_KEEP_NAMES` list and `_BENIGN_WORDS` only override the PATTERN.

    A DENYLIST by design: an allowlist of "safe" names would break
    JAVA_HOME / GOPATH / CARGO_HOME / … in arbitrary projects, and the
    fallback kit has to work in whatever repo it is pointed at. The cost is
    that a credential under an unremarkable name passes — list it in
    secrets.env and it is covered by name."""
    explicit = _explicit_names() if _explicit is None else _explicit
    if name in explicit:
        return True
    if name.upper() in _KEEP_NAMES:
        return False
    return _pattern_match(name)


def scrubbed_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of `base` (default: `os.environ`) with every secret-named
    variable dropped — the environment for any subprocess whose command or
    executed code the MODEL controls. Never mutates `base`."""
    src = os.environ if base is None else base
    explicit = _explicit_names()
    return {k: v for k, v in src.items()
            if not is_secret_name(k, _explicit=explicit)}


# --- known VALUES: redaction + egress refusal (2026-10-22) -------------------

#: (the (value, name) set it was built from, value→name, redaction regex,
#: egress form→name, egress regex). Rebuilt only when the set moves.
_VALUES_CACHE: tuple | None = None


def clear_cache() -> None:
    """Forget every derived cache (the secrets.env parse, compiled patterns)
    so the next call re-reads its sources. Keychain answers are NOT cleared
    here — they belong to `_from_keychain`."""
    global _FILE_CACHE, _VALUES_CACHE
    _FILE_CACHE = None
    _VALUES_CACHE = None


def _collect() -> frozenset[tuple[str, str]]:
    """(value, name) pairs from the three sources, never asking Keychain
    anything new. A PATTERN-matched env variable whose value is a filesystem
    path (`GOOGLE_APPLICATION_CREDENTIALS=/…/creds.json`) is skipped: the
    path is not the secret, and redacting it everywhere would mangle output.
    Explicitly listed values (secrets.env, Keychain, resolved) always count."""
    file_items = _file_items()
    explicit = _explicit_names(file_items)
    pairs: list[tuple[str, str]] = []
    for name in sorted(os.environ):
        value = os.environ[name]
        if not is_secret_name(name, _explicit=explicit):
            continue
        if name not in explicit and value.startswith(("/", "~")):
            continue
        pairs.append((value, name))
    pairs += [(v, k) for k, v in sorted(file_items.items())]
    pairs += [(v, k) for k, v in sorted(_KEYCHAIN_CACHE.items())]
    seen: dict[str, str] = {}
    for value, name in pairs:
        value = value.strip()
        if len(value) >= _MIN_SECRET_LEN:
            seen.setdefault(value, name)
    return frozenset(seen.items())


def _b64_forms(value: str) -> set[str]:
    """The base64 of `value` as it appears at ANY alignment inside a larger
    encoded blob (`Basic base64("user:" + token)`): for each of the three byte
    offsets, the run of output characters that depends only on `value`'s own
    bytes. Standard and URL-safe alphabets."""
    raw = value.encode()
    out: set[str] = set()
    for k in (0, 1, 2):
        data = bytes(k) + raw
        enc = base64.b64encode(data).decode()
        if k == 0:
            out.add(enc.rstrip("="))
        core = enc[4 if k else 0:(len(data) // 3) * 4]
        if len(core) >= _MIN_SECRET_LEN:
            out.add(core)
    return out | {f.replace("+", "-").replace("/", "_") for f in out}


def _alternation(strings) -> re.Pattern:
    """One regex over `strings`, longest first (so a value containing another
    is matched whole)."""
    return re.compile("|".join(re.escape(s) for s in
                               sorted(strings, key=lambda s: (-len(s), s))))


def _tables() -> tuple:
    global _VALUES_CACHE
    pairs = _collect()
    if _VALUES_CACHE is not None and _VALUES_CACHE[0] == pairs:
        return _VALUES_CACHE[1:]
    by_value = dict(pairs)
    forms: dict[str, str] = {}
    for value, name in sorted(by_value.items()):
        forms.setdefault(value, name)
        for f in sorted(_b64_forms(value)):
            forms.setdefault(f, name)
    _VALUES_CACHE = (pairs, by_value,
                     _alternation(by_value) if by_value else None,
                     forms, _alternation(forms) if forms else None)
    return _VALUES_CACHE[1:]


def known_secret_values() -> frozenset[str]:
    """Every secret VALUE luxe can know without asking for more: values of
    secret-named variables in `os.environ`, every value in
    `~/.luxe/secrets.env`, and values ALREADY in `_KEYCHAIN_CACHE` (never a
    new Keychain lookup). Values shorter than 8 characters are ignored.

    Re-derived from its sources on every call (an env scan + one `stat`), so
    it is always current; the file parse and the compiled patterns are cached
    and rebuilt only when the set changes. `clear_cache()` forces a re-read."""
    return frozenset(_tables()[0])


def redact_report(text: str) -> tuple[str, tuple[str, ...]]:
    """`text` with each known secret value replaced by `[REDACTED:<NAME>]`,
    plus the sorted names that fired. Longest value first, so a value that
    contains another is replaced whole. No known value in `text` ⇒ the SAME
    string object back and `()` — byte-identical by construction."""
    if not text:
        return text, ()
    by_value, redact_re, _, _ = _tables()
    if redact_re is None or not redact_re.search(text):
        return text, ()
    fired: set[str] = set()

    def _sub(m: re.Match) -> str:
        name = by_value[m.group(0)]
        fired.add(name)
        return f"[REDACTED:{name}]"
    return redact_re.sub(_sub, text), tuple(sorted(fired))


def redact(text: str) -> str:
    """`redact_report(text)[0]` — what every tool result passes through
    before it reaches the model (agents/loop.py `_dispatch_one`)."""
    return redact_report(text)[0]


def _strings(obj: Any) -> list[str]:
    """Every string leaf (and dict key) of a JSON-shaped value."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, Mapping):
        out: list[str] = []
        for k, v in obj.items():
            out += _strings(k) + _strings(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [s for item in obj for s in _strings(item)]
    return [] if obj is None else [str(obj)]


def secret_in_args(args: Any) -> str | None:
    """The NAME of a known secret whose value the tool-call arguments carry,
    or None. Three forms, deliberately no more: raw, percent-encoded (checked
    by DECODING each argument string, so any hex case and `+`-for-space are
    covered), and base64 at any alignment (standard or URL-safe)."""
    _, _, forms, egress_re = _tables()
    if egress_re is None:
        return None
    for s in _strings(args):
        for candidate in dict.fromkeys((s, urllib.parse.unquote(s),
                                        urllib.parse.unquote_plus(s))):
            m = egress_re.search(candidate)
            if m:
                return forms[m.group(0)]
    return None


def egress_refusal(tool: str, name: str) -> str:
    """The tool result a refused call returns. Names the VARIABLE, never the
    value; says the call did not run and what to do instead."""
    return (f"Refused: the arguments to `{tool}` contain the value of the "
            f"secret `{name}` (raw, percent-encoded or base64). luxe never "
            "lets a known credential leave through a tool call, so this call "
            "was NOT run. Do not retry with the value; if the task genuinely "
            "needs this credential, stop and tell the user.")
