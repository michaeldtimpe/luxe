"""SWE-bench Verified instance → luxe maintain CLI invocation + diff extraction.

PRELIMINARY (2026-05-03). Does NOT include the Docker harness step —
that's `harness.py` (deferred until decision point #1 in the plan
confirms Docker availability). This module produces `predictions.json`
in the SWE-bench harness format, which the harness step then consumes.

Workflow per instance:
1. Ensure a local clone exists at `<work_dir>/<instance_id>/repo`.
2. Reset to `base_commit` (hard) — start from the canonical pre-fix state.
3. Invoke `python -m luxe.cli maintain <repo> <goal> --task bugfix --yes
   --keep-loaded --no-pr` — the agent makes changes and stops at "diff
   produced" (no PR push). The `--no-pr` flag short-circuits the PR step
   for SWE-bench mode (deferred small-CLI-edit; until then we tolerate
   the gh-create failure as in offline mode).
4. Capture `git diff <base_commit> HEAD` as the model_patch.
5. Append a row to predictions.json: `{"instance_id": ..., "model_patch": ...,
   "model_name_or_path": ...}`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fixtures import SweBenchInstance


@dataclass
class SweBenchInvocationResult:
    instance_id: str
    model_patch: str = ""
    wall_s: float = 0.0
    rc: int = 0
    stdout_log: str = ""
    stderr_log: str = ""
    error: str = ""


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None,
         timeout_s: float | None = None) -> tuple[int, str, str]:
    """Subprocess wrapper. Returns (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, check=False,
            env=env, timeout=timeout_s,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        out = (e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes)
               else (e.stdout or ""))
        err = (e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes)
               else (e.stderr or ""))
        err = (err + f"\n[timeout] killed after {timeout_s:.0f}s\n").lstrip()
        return 124, out, err


def ensure_repo(instance: SweBenchInstance, work_dir: Path) -> Path:
    """Ensure the repo for this instance is cloned and reset to base_commit.

    Layout:
        <work_dir>/<instance_id>/repo/  (the clone)
        <work_dir>/<instance_id>/log/   (subprocess logs)

    Returns the repo path.
    """
    inst_dir = work_dir / instance.instance_id
    repo_dir = inst_dir / "repo"
    inst_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / ".git").is_dir():
        rc, out, err = _run(["git", "clone", "--quiet", instance.repo_url, str(repo_dir)])
        if rc != 0:
            raise RuntimeError(f"clone failed for {instance.instance_id}: {err.strip()}")
    # Hard reset to base_commit
    rc, out, err = _run(["git", "fetch", "origin", instance.base_commit], cwd=repo_dir)
    rc, out, err = _run(["git", "reset", "--hard", instance.base_commit], cwd=repo_dir)
    if rc != 0:
        raise RuntimeError(f"reset failed for {instance.instance_id}: {err.strip()}")
    rc, _, _ = _run(["git", "clean", "-fdx"], cwd=repo_dir)
    return repo_dir


# SpecDD Lever 2: synthetic `.sdd` overlay for SWE-bench fixtures.
# The n=75 baseline (2026-05-04) revealed that the anti-reproducer prompt
# rule is leaky — 4/75 instances created `test_fix.py` / `repo_root/...`
# despite the prompt forbidding it. Tool-side enforcement via
# `<repo_basename>.sdd` `Forbids:` is the durable shape: it fires every
# time the model attempts the offending write, regardless of how the
# path was constructed.
#
# Patterns are derived from the literal n=75 leakage cases:
# - test_fix.py / xarray/test_fix.py / sympy/test_det_fix.py (4 cases)
# - repo_root/test_encoded_file.py (1 case)
# Plus prophylactic coverage for adjacent reproducer-shaped names.
SWEBENCH_SDD_BODY = """\
# swebench-fixture

Synthetic contract dropped at fixture-prep time. Tool-side Forbids
enforces the anti-reproducer rule that the prose prompt cannot
strictly hold.

## Forbids
- test_fix.py
- **/test_fix.py
- test_*_fix.py
- **/test_*_fix.py
- repro.py
- **/repro.py
- reproduce.py
- **/reproduce.py
- reproducer.py
- **/reproducer.py
- repo_root/**
- src/test_*.py
- test_encoded_*.py
- **/test_encoded_*.py
"""


def write_swebench_sdd(repo: Path) -> Path:
    """Drop a synthetic `<repo_basename>.sdd` at the cloned-repo root.

    The basename matches the directory name so `find_all_sdd` picks it
    up. Written outside any tracked path; `remove_swebench_sdd` cleans
    it before `extract_diff` so the synthetic contract does not leak
    into the predictions.json patch.
    """
    sdd = repo / f"{repo.name}.sdd"
    sdd.write_text(SWEBENCH_SDD_BODY, encoding="utf-8")
    return sdd


def remove_swebench_sdd(repo: Path) -> None:
    """Remove the synthetic `.sdd` before diff extraction.

    Idempotent: missing file is a no-op.
    """
    sdd = repo / f"{repo.name}.sdd"
    if sdd.is_file():
        sdd.unlink()


def extract_diff(repo: Path, base_commit: str) -> str:
    """`git diff <base_commit> HEAD` — the model patch."""
    rc, out, err = _run(["git", "add", "-N", "."], cwd=repo)
    rc, out, err = _run(["git", "diff", base_commit, "--no-color"], cwd=repo)
    if rc != 0:
        return ""
    return out


def invoke_luxe_maintain(
    instance: SweBenchInstance,
    repo: Path,
    log_dir: Path,
    *,
    config: Path | None = None,
    extra_env: dict[str, str] | None = None,
    timeout_s: float | None = 1800.0,
) -> tuple[int, str, str]:
    """Spawn `luxe maintain` for one SWE-bench instance.

    Returns (rc, stdout, stderr). The agent does its work; the diff is
    later extracted via extract_diff().
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    goal = instance.goal_prompt(max_chars=3000)
    cmd = [
        sys.executable, "-m", "luxe.cli", "maintain",
        str(repo), goal,
        "--task", "bugfix",
        "--yes",
        "--keep-loaded",
    ]
    if config:
        cmd.extend(["--config", str(config)])
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    rc, out, err = _run(cmd, env=env, timeout_s=timeout_s)
    (log_dir / "stdout.log").write_text(out)
    (log_dir / "stderr.log").write_text(err)
    return rc, out, err


def run_instance(
    instance: SweBenchInstance,
    work_dir: Path,
    *,
    config: Path | None = None,
    extra_env: dict[str, str] | None = None,
    timeout_s: float | None = 1800.0,
    inject_sdd: bool = True,
) -> SweBenchInvocationResult:
    """End-to-end per-instance run: ensure repo → inject .sdd → invoke luxe → strip .sdd → extract diff.

    `inject_sdd` (default True) drops a synthetic `<repo_basename>.sdd`
    with anti-reproducer Forbids globs at the cloned-repo root before
    the agent runs, and removes it before diff extraction. Set False to
    reproduce the pre-Lever-2 baseline behaviour.
    """
    inst_dir = work_dir / instance.instance_id
    log_dir = inst_dir / "log"
    t0 = time.monotonic()
    try:
        repo = ensure_repo(instance, work_dir)
    except RuntimeError as e:
        return SweBenchInvocationResult(
            instance_id=instance.instance_id,
            wall_s=time.monotonic() - t0,
            error=f"setup_failed: {e}",
        )
    if inject_sdd:
        write_swebench_sdd(repo)
    try:
        rc, out, err = invoke_luxe_maintain(
            instance, repo, log_dir,
            config=config, extra_env=extra_env, timeout_s=timeout_s,
        )
    finally:
        if inject_sdd:
            remove_swebench_sdd(repo)
    diff = extract_diff(repo, instance.base_commit)
    return SweBenchInvocationResult(
        instance_id=instance.instance_id,
        model_patch=diff,
        wall_s=time.monotonic() - t0,
        rc=rc,
        stdout_log=str(log_dir / "stdout.log"),
        stderr_log=str(log_dir / "stderr.log"),
    )


def write_predictions(
    results: list[SweBenchInvocationResult],
    output_path: Path,
    *,
    model_name: str = "luxe-qwen3.6-35b-a3b-6bit",
) -> None:
    """Emit `predictions.json` in SWE-bench harness format.

    Each result becomes one row: {"instance_id", "model_patch",
    "model_name_or_path"}. Empty patches are still written so the harness
    can grade them as failures.
    """
    import json
    rows = [
        {
            "instance_id": r.instance_id,
            "model_patch": r.model_patch,
            "model_name_or_path": model_name,
        }
        for r in results
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(rows, indent=2))
