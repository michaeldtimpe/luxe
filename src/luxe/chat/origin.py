"""Where is the model I'm talking to? (chat-only provenance)

Two independent facts decide whether a turn crosses the network, and neither
was visible in the UI before 2026-07-29 — a session can stream weights off a
NAS, or run inference on another machine entirely, and look exactly like a
local one:

1. **The endpoint.** `configs/chat.yaml` `backends:` can point at another host
   (`m5` over Tailscale). Then compute AND weights are remote — `kind="remote"`.
2. **The weights.** Even on `localhost`, oMLX serves a `model_path`, and that
   path can live on a network volume (SMB/NFS mount) or inside a cloud-sync
   placeholder tree (`~/Library/CloudStorage/...`). Then loading the model
   streams gigabytes over the wire — `kind="network"`.

Everything here is read-only, guarded, and cached per endpoint: a classification
failure degrades to `kind="unknown"` and never blocks a turn. Chat-only
(chat.sdd) — the benchmark path neither calls nor is affected by it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# The mount(8) parse lives at the root tier (luxe/mounts.py) because
# `modelstore` needs it too and must not import `chat.*`. Re-exported here:
# `classify_path` below resolves `network_mounts` through THIS module's
# globals, so the tests that monkeypatch `origin.network_mounts` still work.
from luxe import textfmt
from luxe.mounts import (  # noqa: F401  (re-exports)
    NETWORK_FSTYPES,
    network_mounts,
    reset_mount_cache,
)

# Cloud-sync providers materialize placeholder trees under here; reads block on
# the provider (the ETIMEDOUT that crashed chat came from exactly this path).
_CLOUD_ROOT = "Library/CloudStorage"

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})

def _elide(path: str, maxlen: int = 72) -> str:
    """Keep a long weight path readable in a one-line notice.

    HF cache layouts end in `snapshots/<40-char-sha>`, which is the least
    informative part — drop it first, and only then middle-ellipsise, so the
    model directory (the part you actually recognise) survives.
    """
    cut = path.find("/snapshots/")
    if cut != -1:
        path = path[:cut]
    if len(path) <= maxlen:
        return path
    keep = maxlen - 1
    return path[: keep // 2] + "…" + path[-(keep - keep // 2):]


@dataclass(frozen=True)
class ModelOrigin:
    """Provenance of one model as served by one endpoint."""

    kind: str = "unknown"   # local | network | remote | unknown
    detail: str = ""        # path, mount source, or host — the evidence
    model_id: str = ""

    @property
    def glyph(self) -> str:
        """One-character status-bar marker."""
        return {"local": "⌂", "network": "☁", "remote": "⇅"}.get(self.kind, "?")

    @property
    def label(self) -> str:
        """Short word for lists (`/model`)."""
        return {"local": "local", "network": "network",
                "remote": "remote"}.get(self.kind, "origin?")

    @property
    def is_over_the_network(self) -> bool:
        return self.kind in ("network", "remote")

    def describe(self) -> str:
        """One-line prose for the startup notice / `/model <slot> <id>`."""
        if self.kind == "local":
            return f"weights on local disk ({_elide(self.detail)})" if self.detail \
                else "weights on local disk"
        if self.kind == "network":
            return f"weights on a NETWORK volume ({self.detail}) — " \
                   "loading streams them over the wire"
        if self.kind == "remote":
            return f"served by a REMOTE host ({self.detail}) — " \
                   "prompts and weights cross the network"
        return "weight location unknown (oMLX did not report a model path)"


# --- endpoint ---------------------------------------------------------------


def endpoint_host(base_url: str) -> str:
    try:
        return (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return ""


def endpoint_is_local(base_url: str) -> bool:
    """True when the oMLX endpoint runs on this machine."""
    host = endpoint_host(base_url)
    if host in _LOCAL_HOSTS:
        return True
    try:
        import socket
        return host in {socket.gethostname().lower(),
                        socket.gethostname().split(".")[0].lower()}
    except OSError:
        return False


# --- local filesystem -------------------------------------------------------

def classify_path(model_path: str | os.PathLike[str] | None) -> ModelOrigin:
    """Classify a LOCAL-endpoint model path as local vs network-backed.

    Symlinks are resolved first: `~/.omlx/models/<id>` is usually a link into
    the HF hub cache, whose snapshot files are in turn links to `blobs/` — so
    the interesting filesystem is the final target's, not the link's.
    """
    if not model_path:
        return ModelOrigin(kind="unknown")
    p = Path(model_path).expanduser()
    try:
        resolved = p.resolve()
    except OSError:
        resolved = p
    text = str(resolved)

    # Cloud-sync placeholder trees (Synology Drive / iCloud / OneDrive / …).
    home = str(Path.home())
    if text.startswith(os.path.join(home, _CLOUD_ROOT)):
        provider = Path(text[len(home) + 1:]).parts[2:3]
        return ModelOrigin(kind="network",
                           detail=f"cloud sync: {provider[0]}" if provider
                           else "cloud sync")

    # Longest matching network mount wins (nested mounts).
    best: tuple[str, str, str] | None = None
    for mp, fstype, src in network_mounts():
        if text == mp or text.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) > len(best[0]):
                best = (mp, fstype, src)
    if best is not None:
        return ModelOrigin(kind="network", detail=f"{best[2]} ({best[1]})")

    return ModelOrigin(kind="local", detail=_tildify(text))


_tildify = textfmt.tilde


# --- endpoint-aware resolution ----------------------------------------------

_origin_cache: dict[str, dict[str, ModelOrigin]] = {}   # base_url -> id -> origin


def reset_cache() -> None:
    _origin_cache.clear()


def origins_for_backend(backend, *, force: bool = False) -> dict[str, ModelOrigin]:
    """Map every model the endpoint serves to its provenance.

    One `/v1/models/status` call per endpoint, cached for the session (model
    paths don't move under a running server). Never raises: an unreachable or
    old server yields `{}` and callers fall back to `unknown`.
    """
    base_url = getattr(backend, "base_url", "") or ""
    if not force and base_url in _origin_cache:
        return _origin_cache[base_url]

    if not endpoint_is_local(base_url):
        # Remote endpoint: the model path it reports is a path over there, so
        # the only honest statement is "this is another machine".
        host = endpoint_host(base_url) or base_url
        origins: dict[str, ModelOrigin] = {}
        try:
            for mid in backend.list_models():
                origins[mid] = ModelOrigin(kind="remote", detail=host, model_id=mid)
        except Exception:
            pass
        _origin_cache[base_url] = origins
        return origins

    origins = {}
    paths = {}
    getter = getattr(backend, "model_paths", None)
    if callable(getter):
        try:
            paths = getter() or {}
        except Exception:
            paths = {}
    for mid, path in paths.items():
        origins[mid] = ModelOrigin(**{**classify_path(path).__dict__,
                                      "model_id": mid})
    _origin_cache[base_url] = origins
    return origins


def origin_for(backend, model_id: str) -> ModelOrigin:
    """Provenance of one model on the active endpoint (`unknown` if unreported).

    May perform one HTTP call (the first time an endpoint is seen) — call it
    from a worker/startup path, never from a render.
    """
    if not model_id:
        return ModelOrigin(kind="unknown")
    found = origins_for_backend(backend).get(model_id)
    return found or ModelOrigin(kind="unknown", model_id=model_id)


def cached_origin_for(backend, model_id: str) -> ModelOrigin:
    """Cache-only variant — safe on the UI thread. Returns `unknown` rather
    than fetching when this endpoint hasn't been probed yet."""
    base_url = getattr(backend, "base_url", "") or ""
    return (_origin_cache.get(base_url, {}).get(model_id)
            or ModelOrigin(kind="unknown", model_id=model_id))
