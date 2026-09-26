"""The ONE fixtures.yaml loader — run.py, scripts/regrade_local.py and
scripts/opencode_harness.py all read fixtures through here.

fixtures.yaml pins absolute fixture-cache paths under one host's home
(`/Users/mtimpe/.luxe/fixture-cache/<repo>`). On a host whose user differs
(m1 is `michaeltimpe`) that path does not exist, and before this module each
reader handled it differently: run.py cloned from the literal path (fresh
clones failed; a reused clone kept working because ITS origin pointed at the
local cache, while `_prune_for_fixture` silently skipped the origin — the
branch backlog in the cache was never pruned); regrade_local.py cloned the
literal path and crashed; only opencode_harness remapped. One loader, one
remap.

`origin_problem` is the preflight: a local origin must exist and be writable,
because every bench run pushes its branch there and `regrade_local.py` grades
the pushed branch. A read-only cache (m1's is `dr-xr-xr-x`) made every push
fail — an ENVIRONMENT failure that used to surface only as a lower score.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import yaml

from benchmarks.maintain_suite.grade import Fixture

DEFAULT_FIXTURES = Path(__file__).parent / "fixtures.yaml"
_CACHE_MARKER = "/.luxe/fixture-cache/"


def remap_repo_url(url: str, home: Path | None = None) -> str:
    """Rewrite a fixture-cache path pinned under another host's home to the
    same cache under THIS user's ~/.luxe, when the pinned path is absent and
    the local one exists. Anything else is returned unchanged."""
    if not url or _CACHE_MARKER not in url or Path(url).exists():
        return url
    local = (home or Path.home()) / ".luxe" / "fixture-cache" / url.split(_CACHE_MARKER, 1)[1]
    return str(local) if local.exists() else url


def load_fixtures(path: Path | None = None, home: Path | None = None) -> list[Fixture]:
    """Parse fixtures.yaml (default: alongside this file) with every
    `repo_url` host-remapped."""
    p = Path(path) if path else DEFAULT_FIXTURES
    raw = yaml.safe_load(p.read_text()) or {}
    out: list[Fixture] = []
    for d in raw.get("fixtures") or []:
        fx = Fixture.from_dict(d)
        url = remap_repo_url(fx.repo_url, home=home)
        out.append(dataclasses.replace(fx, repo_url=url) if url != fx.repo_url else fx)
    return out


def is_local_origin(url: str) -> bool:
    """A filesystem path (what the offline fixture cache uses), not a URL."""
    return bool(url) and "://" not in url and not url.startswith("git@")


def origin_problem(fixture: Fixture) -> str:
    """Why this fixture's origin cannot take a bench push, or "" when it can.

    Only local-path origins are checked (a remote's writability is not
    knowable offline). Writability is checked where a push writes: the
    objects and refs directories of the git dir.
    """
    url = fixture.repo_url
    if fixture.repo_path or not is_local_origin(url):
        return ""
    origin = Path(url).expanduser()
    if not origin.is_dir():
        return (f"{fixture.id}: origin {origin} does not exist (fixtures.yaml "
                "pins another host's path and no ~/.luxe/fixture-cache copy "
                "was found here)")
    gitdir = origin / ".git" if (origin / ".git").is_dir() else origin
    for sub in ("objects", "refs/heads"):
        d = gitdir / sub
        if not d.is_dir():
            return f"{fixture.id}: origin {origin} is not a git repository ({d} missing)"
        if not os.access(d, os.W_OK):
            return (f"{fixture.id}: origin {origin} is read-only ({d}); bench "
                    "pushes would fail. Fix: chmod -R u+w " + str(origin))
    return ""
