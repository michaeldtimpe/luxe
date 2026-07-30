"""Get model weights onto this machine — from HuggingFace or from a mounted
network volume — without leaving luxe.

Two sources, one command (`luxe pull` / `/pull`):

- **HuggingFace** goes through the oMLX admin API (`/admin/api/hf/*`), not a
  second downloader of our own. oMLX already owns a `snapshot_download` worker
  with resume, cancel, and progress, and it writes into the same HF cache its
  loader reads — a parallel implementation would fight it over the cache and
  leave models the server can't see. Admin routes need a session cookie from
  `/admin/api/login` (the `Authorization: Bearer` key alone is rejected), so
  `OmlxAdmin` logs in once and reuses the cookie.
- **A mounted volume** (kappa/alpha over SMB, an external disk) is a plain
  directory copy into the oMLX models dir. When the weights are already on the
  LAN this is far cheaper than re-fetching them from the internet — the reason
  the champion was recovered from the NAS rather than re-downloaded.

Everything here is explicit and user-driven: `pull` is a CLI command and a chat
slash-command, never a tool the model can call. Nothing is deleted, and an
existing model directory is never overwritten unless `force=True`.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import httpx

from luxe.chat.origin import network_mounts
from luxe.fswalk import DEFAULT_SKIP_DIRS

# Where oMLX keeps model directories it serves by name.
DEFAULT_MODELS_DIR = Path.home() / ".omlx" / "models"

# A directory is an MLX model when it has a config and at least one weight file.
_WEIGHT_SUFFIXES = (".safetensors", ".npz", ".gguf")

# Mount scan bounds — a network volume can be enormous and slow; never let
# discovery become the expensive part of a pull.
_SCAN_MAX_DEPTH = 4
_SCAN_BUDGET_S = 20.0


class ModelStoreError(Exception):
    """Any failure fetching or importing model weights."""


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# --- local store ------------------------------------------------------------


def is_model_dir(path: str | os.PathLike[str]) -> bool:
    """True when `path` looks like a loadable MLX model directory."""
    p = Path(path)
    try:
        if not (p / "config.json").is_file():
            return False
        return any(f.suffix in _WEIGHT_SUFFIXES for f in p.iterdir() if f.is_file())
    except OSError:
        return False


def dir_size(path: str | os.PathLike[str]) -> int:
    """Total bytes under `path`, counting what a COPY would actually write.

    Symlinks and Synology `XSym` stubs are measured at their target's size:
    an HF-cache snapshot is nothing but links into `blobs/`, so counting the
    links themselves would report a 27 GB model as a few kilobytes — and both
    the progress bar and the free-space check would be nonsense.
    """
    total = 0
    for cur, _dirs, files in os.walk(path):
        for name in files:
            f = Path(cur) / name
            try:
                target = xsym_target(f)
                if target is not None:
                    resolved = (f.parent / target).resolve()
                    total += resolved.stat().st_size
                else:
                    total += f.stat().st_size      # follows symlinks
            except OSError:
                continue
    return total


# Synology SMB shares expose symlinks as `XSym`-format REGULAR files: a
# fixed 1067-byte blob whose 4th line is the link target. A plain copy of an
# HF-cache snapshot from the NAS therefore yields 1067-byte stubs where the
# weights should be — the exact trap that made a manual champion recovery fail
# (memory: project_champion_weights_recovery_kappa). We resolve them on the way
# in, so an imported model is real files.
_XSYM_MAGIC = b"XSym"
_XSYM_SIZE = 1067


def xsym_target(path: Path) -> str | None:
    """The link target encoded in a Synology `XSym` stub, or None if `path`
    isn't one."""
    try:
        if not path.is_file() or path.stat().st_size != _XSYM_SIZE:
            return None
        head = path.read_bytes()
    except OSError:
        return None
    if not head.startswith(_XSYM_MAGIC):
        return None
    lines = head.split(b"\n")
    if len(lines) < 4:
        return None
    target = lines[3].split(b"\x00")[0].strip().decode("utf-8", "replace")
    return target or None


def _is_org_dir(p: Path) -> bool:
    """A namespace directory, not a model — oMLX's own HF downloads land
    nested as `<store>/<org>/<Name>` (e.g. `mlx-community/Qwen3.6-27B-4bit`),
    and oMLX serves the LEAF name. A namespace has no config.json of its own
    and at least one directory/symlink child; anything else (including an
    empty or broken model dir) is treated as a model entry itself."""
    try:
        if (not p.is_dir() or p.is_symlink()
                or (p / "config.json").exists()):
            return False
        return any(c.is_dir() or c.is_symlink() for c in p.iterdir())
    except OSError:
        return False


def store_entry(name: str, models_dir: Path | None = None) -> Path | None:
    """The store path serving `name`: top-level `<store>/<name>`, else the
    nested `<store>/<org>/<name>` an oMLX HF download creates."""
    root = Path(models_dir or DEFAULT_MODELS_DIR)
    p = root / name
    try:
        if p.is_dir() or p.is_symlink():
            return p
        for sub in root.iterdir():
            if _is_org_dir(sub):
                q = sub / name
                if q.is_dir() or q.is_symlink():
                    return q
    except OSError:
        pass
    return None


def local_model_names(models_dir: Path | None = None) -> list[str]:
    """Model names present in the oMLX store — the names oMLX would serve:
    top-level entries plus the leaf names under org namespace dirs."""
    root = Path(models_dir or DEFAULT_MODELS_DIR)
    names: set[str] = set()
    try:
        for p in root.iterdir():
            if _is_org_dir(p):
                try:
                    names.update(c.name for c in p.iterdir()
                                 if c.is_dir() or c.is_symlink())
                except OSError:
                    continue
            elif p.is_dir() or p.is_symlink():
                names.add(p.name)
    except OSError:
        return []
    return sorted(names)


def model_state(name: str, models_dir: Path | None = None) -> str:
    """Weight presence for one store entry: "ok" | "dangling" | "missing".

    "dangling" is the HF-cache-wipe signature (2026-07-30 finding): the store
    entry EXISTS — usually a symlink into `~/.cache/huggingface` — but the
    weights behind it don't resolve, so listings report a model the server
    cannot actually load. `/doctor`, `luxe smoke`, and `pull --list` all key
    off this instead of bare directory presence.
    """
    p = store_entry(name, models_dir)
    if p is None:
        return "missing"
    return "ok" if is_model_dir(p) else "dangling"


def remove_model(name: str, models_dir: Path | None = None) -> tuple[int, str]:
    """Delete one entry from the local store. Returns (bytes_freed, note).

    A real directory is removed recursively (bytes_freed = its size). A
    symlink is unlinked WITHOUT touching its target — the HF cache behind a
    linked model may back other aliases (and deleting cache bytes is a
    separate, deliberate act). Raises ModelStoreError for an unknown name.
    """
    root = Path(models_dir or DEFAULT_MODELS_DIR)
    p = store_entry(name, root)
    if p is None:
        raise ModelStoreError(f"{name!r} is not in the store ({root})")
    if p.is_symlink():
        target = os.readlink(p)
        p.unlink()
        return 0, f"unlinked (target left alone: {target})"
    freed = dir_size(p)
    shutil.rmtree(p)
    return freed, "removed"


# --- sources ----------------------------------------------------------------


@dataclass(frozen=True)
class ModelSource:
    """Somewhere a model can be pulled from."""

    kind: str                 # "mount" | "hf"
    ref: str                  # filesystem path, or HF repo id
    name: str                 # the name it will have in the oMLX store
    size_bytes: int = 0
    note: str = ""            # mount source / repo detail, for the UI

    def describe(self) -> str:
        size = f", {human_bytes(self.size_bytes)}" if self.size_bytes else ""
        where = "mounted volume" if self.kind == "mount" else "HuggingFace"
        return f"{where}: {self.ref}{size}{f' ({self.note})' if self.note else ''}"


def store_name_for(ref: str) -> str:
    """Local store name for a HF repo id or a path (`org/Model-4bit` → `Model-4bit`)."""
    return ref.rstrip("/").split("/")[-1]


def _matches(dir_name: str, wanted: str) -> bool:
    """Does a directory name identify the wanted model?

    Accepts the exact name, the HF-cache form (`models--org--Name`), and a
    case-insensitive match — NAS layouts vary.
    """
    d, w = dir_name.lower(), wanted.lower()
    return d == w or d.endswith("--" + w) or d == f"models--{w}"


def scan_mounts_for(
    name: str,
    roots: Iterable[str | os.PathLike[str]] | None = None,
    *,
    max_depth: int = _SCAN_MAX_DEPTH,
    budget_s: float = _SCAN_BUDGET_S,
    now: Callable[[], float] = time.monotonic,
) -> list[ModelSource]:
    """Find `name` on mounted network volumes (or explicit `roots`).

    Bounded on purpose: `max_depth` levels and a wall-clock `budget_s`, since
    an unreachable SMB share blocks per-directory. Returns every match, deepest
    metadata included, so the caller can show the user what it found.
    """
    if roots is None:
        roots = [mp for mp, _fs, _src in network_mounts()]
    found: list[ModelSource] = []
    deadline = now() + budget_s
    for root in roots:
        root_path = Path(root)
        base_depth = len(root_path.parts)
        for cur, dirs, _files in os.walk(root_path, onerror=lambda e: None):
            if now() > deadline:
                return found
            depth = len(Path(cur).parts) - base_depth
            if depth >= max_depth:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in DEFAULT_SKIP_DIRS
                       and not d.startswith(".")]
            for d in list(dirs):
                cand = Path(cur) / d
                if not _matches(d, name):
                    continue
                real = _resolve_hf_snapshot(cand)
                if real is not None:
                    found.append(ModelSource(
                        kind="mount", ref=str(real), name=name,
                        size_bytes=dir_size(real), note=str(root_path)))
                    dirs.remove(d)
    return found


def _resolve_hf_snapshot(path: Path) -> Path | None:
    """Return the directory that actually holds weights.

    A NAS copy of an HF cache entry is `models--org--Name/snapshots/<sha>/`;
    a plain export is the model directory itself. Anything else → None.
    """
    if is_model_dir(path):
        return path
    snaps = path / "snapshots"
    try:
        if snaps.is_dir():
            for snap in sorted(snaps.iterdir()):
                if is_model_dir(snap):
                    return snap
    except OSError:
        return None
    return None


# --- importing from a mount -------------------------------------------------


@dataclass
class PullResult:
    ok: bool
    name: str
    source: ModelSource | None = None
    dest: str = ""
    bytes_copied: int = 0
    seconds: float = 0.0
    message: str = ""


def copy_into_store(
    source: ModelSource,
    *,
    models_dir: Path | None = None,
    force: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> PullResult:
    """Copy a mounted model directory into the local oMLX store.

    Writes to a `.partial` sibling and renames on success, so an interrupted
    copy can never leave a half-model that oMLX would try to load. Symlinks are
    dereferenced (an HF-cache copy is all links into `blobs/`, which would be
    dangling here).
    """
    src = Path(source.ref)
    dest_root = Path(models_dir or DEFAULT_MODELS_DIR)
    dest = dest_root / source.name
    if dest.exists() and not force:
        raise ModelStoreError(
            f"{dest} already exists — pass force to replace it")
    if not is_model_dir(src):
        raise ModelStoreError(f"{src} is not an MLX model directory "
                              "(needs config.json + weights)")

    total = source.size_bytes or dir_size(src)
    _check_free_space(dest_root, total)
    staging = dest_root / f".{source.name}.partial"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    copied = 0
    t0 = time.monotonic()
    try:
        for rel, real in _files_to_copy(src):
            out = staging / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(real, out, follow_symlinks=True)
            copied += out.stat().st_size
            if on_progress:
                on_progress(copied, total)
        dest_root.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return PullResult(ok=True, name=source.name, source=source, dest=str(dest),
                      bytes_copied=copied, seconds=time.monotonic() - t0,
                      message=f"copied {human_bytes(copied)} from {src}")


def _files_to_copy(src: Path) -> Iterable[tuple[Path, Path]]:
    """Yield (relative destination, real source file) for everything under
    `src`, dereferencing symlinks AND Synology `XSym` stubs.

    A dangling link is fatal, not silently skipped: importing a model with a
    missing shard produces a directory oMLX will happily list and then fail to
    load — worse than refusing the copy.
    """
    for cur, _dirs, files in os.walk(src):
        for name in sorted(files):
            f = Path(cur) / name
            rel = f.relative_to(src)
            target = xsym_target(f)
            if target is not None:
                real = (f.parent / target).resolve()
                if not real.is_file():
                    raise ModelStoreError(
                        f"{rel}: dangling Synology XSym link → {target} "
                        "(the source copy is incomplete)")
                yield rel, real
                continue
            if f.is_symlink() and not f.exists():
                raise ModelStoreError(f"{rel}: dangling symlink "
                                      "(the source copy is incomplete)")
            yield rel, f


def _check_free_space(dest_root: Path, needed: int) -> None:
    """Refuse a copy that obviously won't fit (keeps 5 GB of headroom)."""
    if not needed:
        return
    probe = dest_root if dest_root.exists() else dest_root.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    if free < needed + 5 * 1024**3:
        raise ModelStoreError(
            f"not enough free space: need {human_bytes(needed)} "
            f"(+5 GB headroom), have {human_bytes(free)}")


# --- HuggingFace, via the oMLX admin API ------------------------------------


@dataclass
class DownloadTask:
    task_id: str = ""
    repo_id: str = ""
    status: str = ""          # pending | downloading | completed | failed | cancelled
    progress: float = 0.0     # percent
    total_size: int = 0
    downloaded_size: int = 0
    error: str = ""

    @property
    def done(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    @classmethod
    def from_dict(cls, d: dict) -> "DownloadTask":
        return cls(
            task_id=d.get("task_id", ""), repo_id=d.get("repo_id", ""),
            status=d.get("status", ""), progress=float(d.get("progress", 0.0) or 0),
            total_size=int(d.get("total_size", 0) or 0),
            downloaded_size=int(d.get("downloaded_size", 0) or 0),
            error=d.get("error", "") or "",
        )


@dataclass
class HFModel:
    """One HuggingFace search hit."""

    repo_id: str = ""
    downloads: int = 0
    likes: int = 0
    size_bytes: int = 0
    tags: list[str] = field(default_factory=list)


class OmlxAdmin:
    """Minimal client for the oMLX admin API (cookie session, not Bearer).

    Only the model-fetch routes are wrapped. Every call raises
    `ModelStoreError` on failure — callers surface it, never a raw httpx error.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8000", api_key: str = "",
                 timeout_s: float = 60.0):
        self.base_url = base_url.rstrip("/")
        from luxe.secrets import resolve_api_key
        self.api_key = api_key or resolve_api_key()
        self._client = httpx.Client(base_url=self.base_url,
                                    timeout=httpx.Timeout(timeout_s, connect=10.0))
        self._logged_in = False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OmlxAdmin":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- plumbing ------------------------------------------------------------

    def login(self) -> None:
        """Exchange the API key for the admin session cookie (idempotent)."""
        if self._logged_in:
            return
        if not self.api_key:
            raise ModelStoreError(
                "no oMLX API key — set OMLX_API_KEY (see ~/.luxe/secrets.env)")
        try:
            r = self._client.post("/admin/api/login", json={"api_key": self.api_key})
        except (httpx.HTTPError, OSError) as e:
            raise ModelStoreError(f"oMLX admin unreachable at {self.base_url}: {e}") from e
        if r.status_code == 401:
            raise ModelStoreError("oMLX rejected the API key (admin login failed)")
        if r.status_code >= 400:
            raise ModelStoreError(f"oMLX admin login failed: {r.status_code} {r.text[:200]}")
        self._logged_in = True

    def _get(self, path: str, **params):
        self.login()
        try:
            r = self._client.get(path, params=params or None)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, OSError, ValueError) as e:
            raise ModelStoreError(f"oMLX admin GET {path} failed: {e}") from e

    def _post(self, path: str, payload: dict | None = None):
        self.login()
        try:
            r = self._client.post(path, json=payload or {})
            if r.status_code >= 400:
                raise ModelStoreError(
                    f"oMLX admin POST {path} failed: {r.status_code} {r.text[:200]}")
            return r.json()
        except (httpx.HTTPError, OSError, ValueError) as e:
            raise ModelStoreError(f"oMLX admin POST {path} failed: {e}") from e

    # -- model fetch ---------------------------------------------------------

    def search(self, query: str, *, limit: int = 20, mlx_only: bool = True,
               sort: str = "downloads") -> list[HFModel]:
        data = self._get("/admin/api/hf/search", q=query, limit=limit,
                         mlx_only=str(mlx_only).lower(), sort=sort)
        out: list[HFModel] = []
        for m in data.get("models", data if isinstance(data, list) else []):
            if not isinstance(m, dict):
                continue
            out.append(HFModel(
                repo_id=m.get("id") or m.get("repo_id") or m.get("modelId", ""),
                downloads=int(m.get("downloads", 0) or 0),
                likes=int(m.get("likes", 0) or 0),
                size_bytes=int(m.get("size") or m.get("size_bytes") or 0),
                tags=list(m.get("tags") or []),
            ))
        return [m for m in out if m.repo_id]

    def model_info(self, repo_id: str) -> dict:
        return self._get("/admin/api/hf/model-info", repo_id=repo_id)

    def start_download(self, repo_id: str, hf_token: str = "") -> DownloadTask:
        data = self._post("/admin/api/hf/download",
                          {"repo_id": repo_id, "hf_token": hf_token})
        task = data.get("task") or {}
        if not task:
            raise ModelStoreError(f"oMLX did not return a download task for {repo_id}")
        return DownloadTask.from_dict(task)

    def tasks(self) -> list[DownloadTask]:
        data = self._get("/admin/api/hf/tasks")
        return [DownloadTask.from_dict(t) for t in data.get("tasks", [])]

    def task(self, task_id: str) -> DownloadTask | None:
        return next((t for t in self.tasks() if t.task_id == task_id), None)

    def cancel(self, task_id: str) -> None:
        self._post(f"/admin/api/hf/cancel/{task_id}")

    def stored_models(self) -> list[dict]:
        """Models on this server's disk, with sizes (`/admin/api/hf/models`)."""
        return self._get("/admin/api/hf/models").get("models", [])

    def wait_for(self, task_id: str, *, poll_s: float = 2.0,
                 on_progress: Callable[[DownloadTask], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> DownloadTask:
        """Poll until the task finishes; report each observation."""
        last = DownloadTask(task_id=task_id, status="pending")
        while True:
            found = self.task(task_id)
            if found is None:
                # Task list rotates completed entries out — treat a vanished
                # task as finished rather than looping forever.
                return last
            last = found
            if on_progress:
                on_progress(found)
            if found.done:
                return found
            sleep(poll_s)


# --- orchestration ----------------------------------------------------------


def resolve_sources(ref: str, *, admin: OmlxAdmin | None = None,
                    mount_roots: Iterable[str] | None = None,
                    include_mounts: bool = True) -> list[ModelSource]:
    """Where could `ref` come from, cheapest first?

    Mounted copies rank above HuggingFace: same bytes, LAN speed, no internet.
    """
    name = store_name_for(ref)
    sources: list[ModelSource] = []
    if include_mounts:
        sources.extend(scan_mounts_for(name, mount_roots))
    if "/" in ref:
        size = 0
        if admin is not None:
            try:
                size = int(admin.model_info(ref).get("size", 0) or 0)
            except ModelStoreError:
                size = 0
        sources.append(ModelSource(kind="hf", ref=ref, name=name, size_bytes=size))
    return sources
