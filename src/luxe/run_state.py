"""Run-state primitives — RunSpec, run directories, event log.

Phase 3 scope: enough state for pr.py to checkpoint commit/push/create/ci-watch
steps and be resumed via `luxe pr <run-id>`. Per-stage pipeline checkpointing
(stage_<n>.json + drift detection on resume) is Phase 4.

Layout under ~/.luxe/runs/<run-id>/:
  run.json      — RunSpec (immutable for the life of the run)
  pr_state.json — pr.py step ledger (mutated as the PR cycle progresses)
  events.jsonl  — append-only log

run_id is a 12-char hex (uuid4 truncated) consistent with PipelineRun.id.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


def runs_root() -> Path:
    return Path.home() / ".luxe" / "runs"


def run_dir(run_id: str) -> Path:
    return runs_root() / run_id


@dataclass
class RunSpec:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    goal: str = ""
    mode: str = "auto"          # auto | single | swarm
    actual_mode: str = ""       # resolved mode (single/swarm)
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
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(json.dumps(spec.to_dict(), indent=2))
    return rd


def load_run_spec(run_id: str) -> RunSpec | None:
    p = run_dir(run_id) / "run.json"
    if not p.is_file():
        return None
    return RunSpec.from_dict(json.loads(p.read_text()))


def save_pr_state(run_id: str, state: PRState) -> None:
    p = run_dir(run_id) / "pr_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state.to_dict(), indent=2))


def load_pr_state(run_id: str) -> PRState | None:
    p = run_dir(run_id) / "pr_state.json"
    if not p.is_file():
        return None
    return PRState.from_dict(json.loads(p.read_text()))


def append_event(run_id: str, kind: str, **data) -> None:
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
