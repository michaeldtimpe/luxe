"""Run-state primitives — RunSpec, run directories, event log.

Layout under ~/.luxe/runs/<run-id>/:
  run.json         — RunSpec (immutable for the life of the run)
  pr_state.json    — pr.py step ledger
  events.jsonl     — append-only log
  synthesizer.md   — final report (kept for `luxe pr <id>` resume)
"""

from __future__ import annotations

from luxe.ephemeral import is_ephemeral
from luxe.paths import luxe_home

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


def runs_root() -> Path:
    return luxe_home() / "runs"


def run_dir(run_id: str) -> Path:
    return runs_root() / run_id


@dataclass
class RunSpec:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    goal: str = ""
    task_type: str = "review"
    repo_path: str = ""
    base_sha: str = ""
    base_branch: str = ""
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RunSpec":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class PRStep:
    name: str           # commit | push | create | watch_ci
    done: bool = False
    status: str = ""    # done | failed | skipped
    detail: str = ""    # error message or short description
    completed_at: float = 0.0


@dataclass
class PRState:
    branch_name: str = ""
    pr_number: int = 0
    pr_url: str = ""
    test_command: str = ""
    test_passed: bool | None = None  # None = not yet run, True/False after
    test_output_tail: str = ""
    is_draft: bool = False
    # The run stopped at its step limit with a change on disk
    # (luxe/steplimit.py): open as a draft, never auto-promote on green CI.
    incomplete: bool = False
    steps: list[PRStep] = field(default_factory=list)

    def step(self, name: str) -> PRStep:
        for s in self.steps:
            if s.name == name:
                return s
        s = PRStep(name=name)
        self.steps.append(s)
        return s

    def is_done(self, name: str) -> bool:
        s = self.step_or_none(name)
        return bool(s and s.done)

    def step_or_none(self, name: str) -> PRStep | None:
        for s in self.steps:
            if s.name == name:
                return s
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PRState":
        steps = [PRStep(**s) for s in d.get("steps", [])]
        d2 = {k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k != "steps"}
        return cls(**d2, steps=steps)


def init_run_dir(spec: RunSpec) -> Path:
    """Create the run directory and write run.json."""
    rd = run_dir(spec.run_id)
    if is_ephemeral():
        return rd
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(json.dumps(spec.to_dict(), indent=2))
    return rd


def load_run_spec(run_id: str) -> RunSpec | None:
    p = run_dir(run_id) / "run.json"
    if not p.is_file():
        return None
    return RunSpec.from_dict(json.loads(p.read_text()))


def save_pr_state(run_id: str, state: PRState) -> None:
    if is_ephemeral():
        return
    p = run_dir(run_id) / "pr_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state.to_dict(), indent=2))


def load_pr_state(run_id: str) -> PRState | None:
    p = run_dir(run_id) / "pr_state.json"
    if not p.is_file():
        return None
    return PRState.from_dict(json.loads(p.read_text()))


def append_event(run_id: str | None, kind: str, **data) -> None:
    """Append one telemetry record to a run's events.jsonl.

    `run_id` is Optional because every caller in `agents/loop.py` reaches
    this through `log_calls` (= `bool(run_id) and ...`), a truthiness mypy
    cannot propagate — 28 of the tracked type errors were that one pattern.
    Widening the parameter and no-opping on a falsy id types those calls
    honestly AND closes the latent bug underneath them: `run_dir(None)`
    would previously have built a directory literally named "None" and
    written telemetry into it.
    """
    if not run_id or is_ephemeral():
        return
    p = run_dir(run_id) / "events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {"kind": kind, "ts": time.time(), "run_id": run_id, **data}
    with p.open("a") as f:
        f.write(json.dumps(record) + "\n")


def list_runs() -> list[RunSpec]:
    out: list[RunSpec] = []
    if not runs_root().is_dir():
        return out
    for d in sorted(runs_root().iterdir()):
        if not d.is_dir():
            continue
        spec_path = d / "run.json"
        if spec_path.is_file():
            try:
                out.append(RunSpec.from_dict(json.loads(spec_path.read_text())))
            except (json.JSONDecodeError, OSError):
                continue
    return out


def gc_runs(retention_days: int = 7) -> int:
    """Remove run directories older than retention_days. Returns count removed."""
    if not runs_root().is_dir():
        return 0
    cutoff = time.time() - (retention_days * 86400)
    removed = 0
    import shutil
    for d in runs_root().iterdir():
        if not d.is_dir():
            continue
        spec_path = d / "run.json"
        if not spec_path.is_file():
            continue
        try:
            spec = RunSpec.from_dict(json.loads(spec_path.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
        if spec.started_at < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


MESSAGES_FILE = "messages.jsonl.gz"


def _messages_path(rd: Path, phase: str) -> Path:
    """`messages.jsonl.gz` for the run's first saved conversation; every
    later `run_agent` under the same run_id (maintain's reprompt / finish
    passes) gets `messages-<phase>.jsonl.gz`, then `-<phase>-<n>`, so no
    pass ever overwrites another's record."""
    first = rd / MESSAGES_FILE
    if not first.exists():
        return first
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in phase) or "run"
    p = rd / f"messages-{safe}.jsonl.gz"
    n = 2
    while p.exists():
        p = rd / f"messages-{safe}-{n}.jsonl.gz"
        n += 1
    return p


def save_messages(run_id: str | None, phase: str,
                  messages: list[dict]) -> Path | None:
    """Write one run's final message list as gzip JSONL (LUXE_SAVE_MESSAGES).

    Write-only telemetry: the caller's list is only read. NEVER raises — a
    failure is logged and skipped, so recording can't change a run's outcome.
    Each line is passed through `luxe.secrets.redact` as a second guard on
    top of the redaction tool results already got in `loop._dispatch_one`.
    Returns the written path, or None (no run_id, ephemeral, or a failure).
    """
    if not run_id or is_ephemeral():
        return None
    try:
        import gzip
        from luxe.secrets import redact
        rd = run_dir(run_id)
        rd.mkdir(parents=True, exist_ok=True)
        p = _messages_path(rd, phase)
        body = "".join(redact(json.dumps(m, default=str)) + "\n"
                       for m in messages)
        tmp = p.with_name(p.name + ".partial")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            f.write(body)
        tmp.replace(p)
        return p
    except Exception as e:  # noqa: BLE001 — recording never fails a run
        import logging
        logging.getLogger(__name__).warning(
            "save_messages(%s, %s) skipped: %s", run_id, phase, e)
        return None
