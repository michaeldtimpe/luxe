"""Persistent session storage — JSONL at ~/.luxe/sessions/<id>.jsonl.

Append-only so a crash mid-turn doesn't corrupt earlier history.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _slug(text: str, limit: int = 40) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return text[:limit] or "session"


@dataclass
class Session:
    path: Path
    session_id: str

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

    def append(self, event: dict[str, Any]) -> None:
        event = {"ts": dt.datetime.now().isoformat(timespec="seconds"), **event}
        with self.path.open("a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
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


def list_sessions(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.glob("*.jsonl"), reverse=True)


def latest_session(root: Path) -> Path | None:
    sessions = list_sessions(root)
    return sessions[0] if sessions else None
