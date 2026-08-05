"""Which filesystems on this machine come over the wire.

One `mount(8)` parse, cached per process. Lives at the root tier rather than
under `chat/` because BOTH `chat.origin` (is my model's weight path on a NAS?)
and `modelstore` (which mounted volumes should a `luxe pull` search?) need it —
`modelstore` importing `chat.origin` was the repo's only low-level→chat import
(broken 2026-08-04). `chat.origin` re-exports these names, so its callers and
the tests that monkeypatch them there are unaffected.

There is no stdlib call that reports a filesystem's type and psutil is not a
dependency, hence the subprocess. A failure degrades to "no network mounts";
nothing here ever raises.
"""

from __future__ import annotations

import re
import subprocess

# Filesystem types that mean "the bytes come over the wire".
NETWORK_FSTYPES = frozenset({
    "smbfs", "nfs", "afpfs", "cifs", "webdav", "ftp", "sshfs", "osxfuse",
})

_MOUNT_RE = re.compile(r"^(?P<src>.+?) on (?P<mp>.+?) \((?P<opts>[^)]*)\)\s*$")

_mounts: list[tuple[str, str, str]] | None = None


def network_mounts(*, force: bool = False) -> list[tuple[str, str, str]]:
    """Network mount points on THIS machine, as (mountpoint, fstype, source).

    Parsed from `mount(8)` once per process — there is no stdlib call that
    reports a filesystem's type, and psutil is not a dependency.
    """
    global _mounts
    if _mounts is not None and not force:
        return _mounts
    out: list[tuple[str, str, str]] = []
    try:
        proc = subprocess.run(["/sbin/mount"], capture_output=True, text=True,
                              timeout=5)
        for line in proc.stdout.splitlines():
            m = _MOUNT_RE.match(line.strip())
            if not m:
                continue
            fstype = m.group("opts").split(",")[0].strip().lower()
            if fstype in NETWORK_FSTYPES:
                out.append((m.group("mp"), fstype, m.group("src")))
    except (OSError, subprocess.SubprocessError):
        out = []
    _mounts = out
    return out


def reset_mount_cache() -> None:
    global _mounts
    _mounts = None
