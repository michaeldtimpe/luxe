"""Chat session persistence — transcripts that can be replayed/resumed.

Layout under ~/.luxe/sessions/<session_id>/:
  meta.json        — SessionMeta (immutable for the life of the session)
  transcript.jsonl — append-only conversational log (user/assistant/tool/event)
  fold.jsonl       — append-only record of the summarizer output per turn
                     (which SUMMARIZER_VERSION produced the injected context)

Mirrors the idioms in src/luxe/run_state.py; this is the *conversational* view,
while ~/.luxe/runs/<run_id>/events.jsonl stays the agent-internal view.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


def sessions_root() -> Path:
    return Path.home() / ".luxe" / "sessions"


def session_dir(session_id: str) -> Path:
    return sessions_root() / session_id


@dataclass
class SessionMeta:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    repo_path: str = ""
    project_hash: str = ""
    config_path: str = ""
    # Resolved model per slot at session start (chat/plan/code) — informational
    # so a resumed session shows what drove it.
    slot_models: dict[str, str] = field(default_factory=dict)
    title: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionMeta":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _meta_path(session_id: str) -> Path:
    return session_dir(session_id) / "meta.json"


def _write_meta(meta: SessionMeta) -> None:
    p = _meta_path(meta.session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta.to_dict(), indent=2))
    tmp.replace(p)


def new_session(
    *,
    repo_path: str = "",
    project_hash: str = "",
    config_path: str = "",
    slot_models: dict[str, str] | None = None,
    title: str = "",
) -> SessionMeta:
    meta = SessionMeta(
        repo_path=repo_path,
        project_hash=project_hash,
        config_path=config_path,
        slot_models=dict(slot_models or {}),
        title=title,
    )
    session_dir(meta.session_id).mkdir(parents=True, exist_ok=True)
    _write_meta(meta)
    return meta


def touch(session_id: str) -> None:
    """Bump last_active (called as turns are appended)."""
    meta = load_meta(session_id)
    if meta is None:
        return
    meta.last_active = time.time()
    _write_meta(meta)


def append_turn(session_id: str, kind: str, **data) -> None:
    """Append one record to transcript.jsonl. `kind` is user|assistant|tool|event."""
    p = session_dir(session_id) / "transcript.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {"kind": kind, "ts": time.time(), **data}
    with p.open("a") as f:
        f.write(json.dumps(record) + "\n")


def append_fold(session_id: str, turn_idx: int, version: str, text: str) -> None:
    """Record the summarizer output that fed turn `turn_idx`'s context."""
    p = session_dir(session_id) / "fold.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {"turn_idx": turn_idx, "version": version, "ts": time.time(), "text": text}
    with p.open("a") as f:
        f.write(json.dumps(record) + "\n")


def load_meta(session_id: str) -> SessionMeta | None:
    p = _meta_path(session_id)
    if not p.is_file():
        return None
    try:
        return SessionMeta.from_dict(json.loads(p.read_text()))
    except (json.JSONDecodeError, OSError):
        return None


def load_session(session_id: str) -> tuple[SessionMeta, list[dict]] | None:
    """Return (meta, transcript records) or None if the session is missing."""
    meta = load_meta(session_id)
    if meta is None:
        return None
    records: list[dict] = []
    tp = session_dir(session_id) / "transcript.jsonl"
    if tp.is_file():
        for line in tp.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return meta, records


def list_sessions() -> list[SessionMeta]:
    out: list[SessionMeta] = []
    if not sessions_root().is_dir():
        return out
    for d in sessions_root().iterdir():
        if not d.is_dir():
            continue
        meta = load_meta(d.name)
        if meta is not None:
            out.append(meta)
    # Most-recently-active first.
    out.sort(key=lambda m: m.last_active, reverse=True)
    return out


def gc_sessions(*, keep_recent: int = 50, retention_days: int = 30) -> int:
    """Evict old sessions. Removes a session if it is BOTH outside the
    `keep_recent` most-recent set AND older than `retention_days`.

    Returns the count removed. Defaults: keep the 50 most recent, drop anything
    older than 30 days (whichever removes more) — defined per memory.sdd so the
    function doesn't bitrot.
    """
    metas = list_sessions()  # already sorted most-recent-first
    if not metas:
        return 0
    cutoff = time.time() - (retention_days * 86400)
    keep_ids = {m.session_id for m in metas[:keep_recent]}
    removed = 0
    for m in metas:
        if m.session_id in keep_ids:
            continue
        if m.last_active >= cutoff:
            continue
        shutil.rmtree(session_dir(m.session_id), ignore_errors=True)
        removed += 1
    return removed
