"""Task / Subtask data model + disk persistence.

Subtasks carry luxe's structured ToolCall values during execution; they are
serialized to plain dicts on write so state.json stays JSON-clean. On
load we rehydrate them back into ToolCall instances.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import string
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from harness.backends import ToolCall

TASKS_ROOT = Path.home() / ".luxe" / "tasks"


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _short_id(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def task_id() -> str:
    return f"T-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{_short_id()}"


def subtask_id(parent: str, index: int) -> str:
    return f"{parent}.{index:02d}"


@dataclass
class Subtask:
    id: str
    parent_id: str
    index: int
    title: str
    agent: str = ""                    # "" = router chooses at dispatch time
    status: str = "pending"            # pending / running / done / blocked / skipped
    attempt: int = 0
    created_at: str = field(default_factory=_now)
    started_at: str = ""
    completed_at: str = ""
    result_text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_calls_total: int = 0
    steps_taken: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    wall_s: float = 0.0
    error: str = ""

    def short(self) -> str:
        icons = {
            "pending": "○", "running": "►", "done": "✓",
            "blocked": "⚠", "skipped": "–",
        }
        return f"  {icons.get(self.status, '?')} [{self.id}] {self.title}"


@dataclass
class Task:
    id: str
    goal: str
    created_at: str = field(default_factory=_now)
    subtasks: list[Subtask] = field(default_factory=list)
    status: str = "planning"           # planning / running / done / blocked / aborted
    completed_at: str = ""
    max_wall_s: float = 3600.0         # overall job cap; scope this up for big jobs
    retry_on_transport_error: bool = True
    pid: int = 0                       # subprocess pid when running in background

    def dir(self) -> Path:
        return TASKS_ROOT / self.id

    def finished(self) -> bool:
        return self.status in ("done", "blocked", "aborted")

    def is_alive(self) -> bool:
        """True if a background subprocess is still running this task."""
        if self.pid <= 0:
            return False
        try:
            import os
            os.kill(self.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but belongs to another user — treat as alive.
            return True


def _tc_to_dict(tc: ToolCall) -> dict[str, Any]:
    return {
        "id": tc.id,
        "name": tc.name,
        "arguments": tc.arguments,
        "raw_arguments": tc.raw_arguments,
    }


def _tc_from_dict(d: dict[str, Any]) -> ToolCall:
    return ToolCall(
        id=d.get("id", ""),
        name=d.get("name", ""),
        arguments=d.get("arguments", {}),
        raw_arguments=d.get("raw_arguments", ""),
    )


def _subtask_to_dict(s: Subtask) -> dict[str, Any]:
    d = asdict(s)
    d["tool_calls"] = [_tc_to_dict(tc) for tc in s.tool_calls]
    return d


def _subtask_from_dict(d: dict[str, Any]) -> Subtask:
    tcs_raw = d.pop("tool_calls", []) or []
    tcs = [_tc_from_dict(t) if isinstance(t, dict) else t for t in tcs_raw]
    s = Subtask(**d)
    s.tool_calls = tcs
    return s


def persist(task: Task) -> None:
    d = task.dir()
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": task.id,
        "goal": task.goal,
        "created_at": task.created_at,
        "subtasks": [_subtask_to_dict(s) for s in task.subtasks],
        "status": task.status,
        "completed_at": task.completed_at,
        "max_wall_s": task.max_wall_s,
        "retry_on_transport_error": task.retry_on_transport_error,
        "pid": task.pid,
    }
    tmp = d / "state.json.tmp"
    with tmp.open("w") as f:
        json.dump(payload, f, indent=2, default=str)
    tmp.replace(d / "state.json")


def load(task_id_full: str) -> Task | None:
    path = TASKS_ROOT / task_id_full / "state.json"
    if not path.exists():
        return None
    with path.open() as f:
        data = json.load(f)
    subs_raw = data.pop("subtasks", []) or []
    subs = [_subtask_from_dict(s) for s in subs_raw]
    return Task(subtasks=subs, **data)


def list_all(limit: int | None = None) -> list[Task]:
    if not TASKS_ROOT.exists():
        return []
    dirs = sorted(TASKS_ROOT.iterdir(), reverse=True)
    out: list[Task] = []
    for d in dirs:
        if not d.is_dir():
            continue
        t = load(d.name)
        if t:
            out.append(t)
        if limit and len(out) >= limit:
            break
    return out


def append_log_event(task: Task, event: dict[str, Any]) -> None:
    """Structured progress record — one JSON object per line. The background
    REPL watcher (Phase 2) tails this to stream status without re-parsing
    state.json on every tick."""
    d = task.dir()
    d.mkdir(parents=True, exist_ok=True)
    event = {"ts": _now(), **event}
    with (d / "log.jsonl").open("a") as f:
        f.write(json.dumps(event, default=str) + "\n")


def resolve_partial(partial: str) -> Task | None:
    """Let the user type a prefix of a task id. Returns None if ambiguous."""
    if not TASKS_ROOT.exists():
        return None
    matches = [d for d in TASKS_ROOT.iterdir() if d.is_dir() and d.name.startswith(partial)]
    if len(matches) != 1:
        return None
    return load(matches[0].name)
