"""Persistent session storage — JSONL at ~/.luxe/sessions/<id>.jsonl.

Append-only so a crash mid-turn doesn't corrupt earlier history.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


def _slug(text: str, limit: int = 40) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return text[:limit] or "session"


@dataclass
class Session:
    path: Path
    session_id: str
    # Active provider/base_url injected into every append() call inside a
    # bind_backend() context. Keeps router.py and agents/base.py from
    # having to plumb backend metadata through 15+ append callsites.
    _backend_kind: str | None = field(default=None, repr=False)
    _backend_url: str | None = field(default=None, repr=False)

    @classmethod
    def new(cls, root: Path, first_prompt: str = "") -> Session:
        root.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        slug = _slug(first_prompt) if first_prompt else "session"
        session_id = f"{ts}-{slug}"
        return cls(path=root / f"{session_id}.jsonl", session_id=session_id)

    @classmethod
    def load(cls, path: Path) -> Session:
        return cls(path=path, session_id=path.stem)

    @contextmanager
    def bind_backend(self, kind: str, base_url: str) -> Iterator[None]:
        """Tag every append() inside this block with provider + base_url.

        Nested binds restore the outer values on exit, so a sub-agent
        running on a different backend doesn't bleed metadata into its
        parent's events.
        """
        old_kind, old_url = self._backend_kind, self._backend_url
        self._backend_kind, self._backend_url = kind, base_url
        try:
            yield
        finally:
            self._backend_kind, self._backend_url = old_kind, old_url

    def append(self, event: dict[str, Any]) -> None:
        event = {"ts": dt.datetime.now().isoformat(timespec="seconds"), **event}
        # Don't overwrite explicit values in the event — caller intent wins.
        if self._backend_kind and "provider" not in event:
            event["provider"] = self._backend_kind
        if self._backend_url and "base_url" not in event:
            event["base_url"] = self._backend_url
        with self.path.open("a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            events: list[dict[str, Any]] = []
            with self.path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            return events
        except OSError:
            return []

    def prune(self, max_turns: int = 200) -> int:
        """Keep only the last `max_turns` events. Returns # removed.

        Atomic: writes to a sibling tempfile and renames, so a crash mid-prune
        leaves the original intact."""
        events = self.read_all()
        if len(events) <= max_turns:
            return 0
        kept = events[-max_turns:]
        tmp = self.path.with_suffix(".jsonl.tmp")
        with tmp.open("w") as f:
            for e in kept:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        tmp.replace(self.path)
        return len(events) - len(kept)


def list_sessions(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.glob("*.jsonl"), reverse=True)


def latest_session(root: Path) -> Path | None:
    sessions = list_sessions(root)
    return sessions[0] if sessions else None
