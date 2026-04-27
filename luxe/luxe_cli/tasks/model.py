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
    near_cap_turns: int = 0  # turns that used ≥80% of per-turn token cap
    # Count of tool-call attempts rejected by client-side JSONSchema
    # validation before reaching the fn. Surfaced so repeated malformed
    # calls are visible without digging through session logs.
    schema_rejects: int = 0
    wall_s: float = 0.0
    error: str = ""
    # Optional per-subtask override. Takes precedence over the agent's
    # static config. Used when a specific subtask has different needs
    # — e.g. the synthesis subtask wants more output tokens because
    # it assembles the final report.
    max_tokens_per_turn_override: int | None = None

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
    # Languages the dispatched review/refactor agent's analyzer surface
    # should target. Populated from RepoSurvey.language_breakdown in
    # /review + /refactor so a pure-Python repo doesn't see
    # lint_js/typecheck_ts/lint_rust/vet_go in its tool list. None = no
    # filter (full surface). Stored as a list so state.json stays clean;
    # converted to frozenset at dispatch.
    analyzer_languages: list[str] | None = None

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
        "wall_s": round(tc.wall_s, 3),
        "ok": tc.ok,
        "bytes_out": tc.bytes_out,
    }


def _tc_from_dict(d: dict[str, Any]) -> ToolCall:
    return ToolCall(
        id=d.get("id", ""),
        name=d.get("name", ""),
        arguments=d.get("arguments", {}),
        raw_arguments=d.get("raw_arguments", ""),
        wall_s=float(d.get("wall_s", 0.0)),
        ok=bool(d.get("ok", True)),
        bytes_out=int(d.get("bytes_out", 0)),
    )


def _subtask_to_dict(s: Subtask) -> dict[str, Any]:
    d = asdict(s)
    d["tool_calls"] = [_tc_to_dict(tc) for tc in s.tool_calls]
    return d


def _subtask_from_dict(d: dict[str, Any]) -> Subtask:
    tcs_raw = d.pop("tool_calls", []) or []
    tcs = [_tc_from_dict(t) if isinstance(t, dict) else t for t in tcs_raw]
    # Older state.json files (pre fixed-per-mode ctx) carried this
    # field; drop it on load so the Subtask init doesn't error.
    d.pop("num_ctx_override", None)
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
        "analyzer_languages": task.analyzer_languages,
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
    # Older state.json files (pre fixed-per-mode ctx) carried this
    # field; drop it on load so the Task init doesn't error.
    data.pop("num_ctx_override", None)
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


def reset_incomplete_subtasks(task: Task) -> int:
    """Flip blocked/skipped/running subtasks back to pending so the
    orchestrator will re-execute them on the next run. Done subtasks
    are untouched — their result_text is re-used by _augment_with_prior.
    Returns the count reset."""
    reset = 0
    for s in task.subtasks:
        if s.status in ("blocked", "skipped", "running"):
            s.status = "pending"
            s.attempt = 0
            s.started_at = ""
            s.completed_at = ""
            s.error = ""
            s.result_text = ""
            s.tool_calls = []
            s.tool_calls_total = 0
            s.steps_taken = 0
            s.prompt_tokens = 0
            s.completion_tokens = 0
            s.near_cap_turns = 0
            s.schema_rejects = 0
            s.wall_s = 0.0
            reset += 1
    if reset:
        task.status = "planning"
        task.completed_at = ""
        task.pid = 0
        persist(task)
    return reset


def resolve_partial(partial: str) -> Task | None:
    """Let the user type a prefix of a task id. Returns None if ambiguous."""
    if not TASKS_ROOT.exists():
        return None
    matches = [d for d in TASKS_ROOT.iterdir() if d.is_dir() and d.name.startswith(partial)]
    if len(matches) != 1:
        return None
    return load(matches[0].name)
