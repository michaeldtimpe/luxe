"""luxe's own state directory.

`~/.luxe` was spelled out at twelve places across the package — runs, locks,
sessions, memory, reports, compare, cohort history, the CVE cache, the theme
preference, the secrets file, the MCP audit log, the netwatch log. One
function, so the answer to "where does luxe keep its state" is in one place.

Deliberately NOT configurable: making the root an env var or a setting would
turn a refactor into a feature, and every one of those paths is also written
down in OUTAGE.md and the docs.
"""

from __future__ import annotations

from pathlib import Path


def luxe_home() -> Path:
    """`~/.luxe` — the root of everything luxe persists on this host.

    Resolved on each call rather than cached at import, so a test that
    monkeypatches `$HOME` (or `Path.home`) sees its own directory.
    """
    return Path.home() / ".luxe"
