"""Run-state primitives — RunSpec, stage checkpoints, run directories, event log.

Layout under ~/.luxe/runs/<run-id>/:
  run.json         — RunSpec (immutable for the life of the run)
  stages/<n>.json  — per-stage checkpoint outputs (architect, worker_<i>,
                     validator, synthesizer)
  pr_state.json    — pr.py step ledger
  events.jsonl     — append-only log
  synthesizer.md   — final report (also stored as a stage; this is a
                     convenience copy for `luxe pr <id>` resume)

run_id is a 12-char hex (uuid4 truncated) consistent with PipelineRun.id.

Resume model:
- `load_stage(run_id, name)` returns the saved dict or None.
- The orchestrator checks each stage on entry; if a checkpoint exists, it
  loads the result and skips that stage.
- `clear_stages(run_id)` is invoked by --force-resume to invalidate the
  cache when HEAD has drifted from RunSpec.base_sha.
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
    execution_mode: str = "swarm"  # swarm | microloop

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


def stages_dir(run_id: str) -> Path:
    return run_dir(run_id) / "stages"


def save_stage(run_id: str, name: str, data: dict) -> Path:
    """Write a stage checkpoint to stages/<name>.json (atomic write)."""
    d = stages_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(p)
    return p


def load_stage(run_id: str, name: str) -> dict | None:
    p = stages_dir(run_id) / f"{name}.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def blackboard_dir(run_id: str) -> Path:
    return run_dir(run_id) / "blackboard"


def save_blackboard(run_id: str, subtask_idx: int, data: dict) -> Path:
    """Atomic-write a microloop blackboard for one subtask.

    Layout: ~/.luxe/runs/<run_id>/blackboard/<subtask_idx>.json
    Mirrors save_stage's tmp+rename atomicity.
    """
    d = blackboard_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{subtask_idx}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(p)
    return p


_BLACKBOARD_REQUIRED_KEYS = {"subtask_idx", "version", "micro_steps"}
_BLACKBOARD_KNOWN_VERSIONS = {1}


def _validate_blackboard_shape(data: object) -> tuple[bool, str]:
    """Minimal schema check — protects against corrupt writes or hallucinated
    structure quietly poisoning downstream micro-steps.

    Returns (ok, error_message). Cheap structural check; not a full schema
    validator. The microloop runner is the only writer, so this is mostly a
    defence against partial-write corruption (atomic rename should prevent
    that, but the read path stays paranoid).
    """
    if not isinstance(data, dict):
        return False, f"top-level must be dict, got {type(data).__name__}"
    missing = _BLACKBOARD_REQUIRED_KEYS - set(data.keys())
    if missing:
        return False, f"missing required keys: {sorted(missing)}"
    if data.get("version") not in _BLACKBOARD_KNOWN_VERSIONS:
        return False, f"unknown blackboard version: {data.get('version')!r}"
    if not isinstance(data.get("micro_steps"), list):
        return False, "micro_steps must be a list"
    return True, ""


def load_blackboard(run_id: str, subtask_idx: int) -> dict | None:
    """Read a blackboard JSON. Returns None if the file is missing, corrupt,
    or fails schema validation — the microloop runner treats None as "no
    prior state" and resumes from a fresh slate, which is safer than
    propagating a bad blackboard through downstream micro-steps.
    """
    p = blackboard_dir(run_id) / f"{subtask_idx}.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return None
    ok, _err = _validate_blackboard_shape(data)
    if not ok:
        return None
    return data


def list_completed_stages(run_id: str) -> list[str]:
    d = stages_dir(run_id)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def clear_stages(run_id: str) -> int:
    """Delete all stage checkpoints for a run. Returns count removed."""
    d = stages_dir(run_id)
    if not d.is_dir():
        return 0
    removed = 0
    for p in d.glob("*.json"):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


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
