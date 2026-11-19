"""Per-invocation run records for gitkit (gitkit.sdd § Must: run record).

Every gitkit invocation mints ONE run id and writes
`~/.luxe/runs/<id>/run.json`; every model pass of that invocation logs under
that one top-level dir. A single-pass report/brief turn logs at
`<id>/events.jsonl`; each further or staged pass (plan-extract, deep
survey/chunk-N/reduce-N/synthesis, apply step-<id>) logs under its own
sub-id `<id>/<label>` -> `<id>/<label>/events.jsonl`. The sub-id is the
`run_id` `run_single` already takes, so pass attribution needs no new
parameter and every event record names its pass; `list_runs`/`gc_runs` see
one entry per invocation (they walk top-level dirs with a run.json).

`--no-save` returns no id at all (nothing under `~/.luxe/runs/`, gitkit.sdd
"write nothing under ~/.luxe/"); `--ephemeral` is honored by
`run_state.init_run_dir` / `append_event` themselves.
"""

from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timezone

from luxe import run_state


def mint_run_id(kind: str) -> str:
    """`gitkit-<kind>-<utc yyyymmddThhmmssZ>-<6 hex>`."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"gitkit-{kind}-{ts}-{secrets.token_hex(3)}"


def start_run(kind: str, *, target: str, head: str, mode: str, model: str,
              task_type: str = "", save: bool = True,
              extra: dict | None = None) -> str | None:
    """Mint the invocation's run id and write its run.json. Returns None when
    `save` is False (callers pass that straight to `run_single(run_id=)`,
    which then logs nothing)."""
    if not save:
        return None
    now = time.time()
    rid = mint_run_id(kind)
    spec = run_state.RunSpec(
        run_id=rid, goal=f"gitkit {kind}", task_type=task_type,
        repo_path=str(target), base_sha=head or "", started_at=now)
    run_state.init_run_dir(spec, extra={
        "tool": "gitkit", "kind": kind, "mode": mode, "model": model,
        "head_sha": head or "",
        "started_at_utc": datetime.fromtimestamp(
            now, timezone.utc).isoformat(timespec="seconds"),
        **(extra or {})})
    return rid


_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def pass_id(run_id: str | None, label: str) -> str | None:
    """The sub-id one pass of a multi-pass invocation logs under, or None when
    the invocation has no record. `label` becomes ONE path component: apply
    step ids come from model-written plan JSON, so anything outside
    `[A-Za-z0-9_-]` (incl. `/` and `.`) is replaced — no traversal out of the
    invocation's dir."""
    if not run_id:
        return None
    return f"{run_id}/{_UNSAFE.sub('_', label) or '_'}"
