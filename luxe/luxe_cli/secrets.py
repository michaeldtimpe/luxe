"""Load persistent secrets (API keys) from ~/.luxe/secrets.env into
`os.environ` at startup, so the user doesn't have to `export
OMLX_API_KEY=...` in every shell that launches luxe.

Format: shell-style `KEY=VALUE` lines, `#` for comments, `KEY=` to
intentionally clear a key. Existing values in `os.environ` ALWAYS
win — a one-shot `OMLX_API_KEY=foo luxe` invocation overrides the
file, and a value already exported in the user's shell wins too.

The file is *strictly* local — `.gitignore` covers `~/.luxe/`. A
template ships at `luxe/daily_driver/secrets.env.example`.
"""

from __future__ import annotations

import os
from pathlib import Path

SECRETS_PATH = Path.home() / ".luxe" / "secrets.env"


def load_secrets(path: Path = SECRETS_PATH) -> dict[str, str]:
    """Read `path` (KEY=VALUE per line, `#` comments) and inject any
    missing entries into `os.environ`. Returns the dict of keys this
    call actually loaded — useful for tests + the startup status line."""
    if not path.exists():
        return {}
    loaded: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        # Strip optional surrounding quotes so the file can be either
        # `KEY=value` or `KEY="value"` (matching common shell .env
        # conventions).
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        # Explicit shell exports + one-shot env-var prefixes both win
        # over the file — that lets a user override per-invocation
        # without having to edit secrets.env.
        if key not in os.environ:
            os.environ[key] = val
            loaded[key] = val
    return loaded


def warn_missing_omlx_key(cfg) -> str | None:
    """Return a one-line warning if any agent points at an oMLX
    endpoint (port 8000) but `OMLX_API_KEY` is unset. Returns None
    when everything's wired up. Caller decides where to render —
    typically the REPL banner."""
    if os.environ.get("OMLX_API_KEY"):
        return None
    omlx_agents = []
    for agent in (getattr(cfg, "agents", None) or []):
        endpoint = getattr(agent, "endpoint", None) or ""
        # Heuristic: oMLX defaults to port 8000. If a different port is
        # used, the user has explicitly opted in and we trust them to
        # know whether auth is required.
        if ":8000" in endpoint:
            omlx_agents.append(agent.name)
    if not omlx_agents:
        return None
    return (
        f"OMLX_API_KEY not set, but agent(s) {omlx_agents} use oMLX. "
        f"Calls will 401. Set the key in ~/.luxe/secrets.env "
        f"(template: daily_driver/secrets.env.example) or `export` it."
    )
