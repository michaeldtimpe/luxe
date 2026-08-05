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
"""

from __future__ import annotations

from luxe.paths import luxe_home

import os
import subprocess

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


def _from_keychain(name: str) -> str:
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", name, "-w"],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def resolve_api_key(env_name: str = "OMLX_API_KEY") -> str:
    """The API key for `env_name`, from env → secrets.env → keychain."""
    return (os.environ.get(env_name, "")
            or _from_file(env_name)
            or _from_keychain(env_name))
