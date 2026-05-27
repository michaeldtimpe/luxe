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
#
# v1.6 (2026-05-06): split into two policy classes after v1.5's
# broad-glob approach hit an architectural ceiling.
#
# `Forbids` fires on every write (create or edit). `Forbids creating`
# fires only when the write would create a new file at that path —
# i.e., the model cannot invent new validation scaffolding, but may
# freely edit any pre-existing repo file even if its name happens to
# match a creation-banned pattern.
#
# Why the split: v1.5 broad globs (`**/*_verify.py`, `**/test_*.py`)
# could not safely deploy as edit-or-create bans because they would
# block legitimate edits to existing repo files. Under create-only
# semantics they become principled — *the model may not invent new
# tests; it may freely edit existing ones*. The Phase A static audit
# (full SWE-bench Verified, n=500, 2026-05-06) confirmed zero gold
# patches create a `test_*.py` file, so `**/test_*.py` ships as a
# stable adapter-wide create-ban.
#
# History:
#   - n=75 baseline (2026-05-04): observed 4 instances create
#     test_fix.py / xarray/test_fix.py / sympy/test_det_fix.py + 1
#     repo_root/test_encoded_file.py.
#   - v1.5 paired-mechanism rerun (2026-05-06,
#     post_specdd_v15_pressure_n75/rep_1): 8 new escapes under
#     WRITE_PRESSURE actuation (verify_fix / tmp_* / test_*_verify /
#     test_*_time / verify_* shapes).
#   - v2 rerun with broad-glob tightening (2026-05-06,
#     post_specdd_v15_pressure_v2_n75): cratered 8 → 2 but two novel
#     escapes (test_bool_contour.py, test_fix_check.py) showed that
#     `test_<topic>.py` is indistinguishable from a legitimate test
#     file — no safe broad glob exists. v1.6's create-only semantics
#     resolve the paradox.
SWEBENCH_SDD_BODY = """\
# swebench-fixture

Synthetic contract dropped at fixture-prep time. Tool-side enforcement
backs the anti-reproducer prompt rule that the prose layer cannot
strictly hold.

## Notes
- `Forbids` (create-or-edit): only `repo_root/**` — synthetic prompt
  context that should never appear as a real repo path under any
  operation.
- `Forbids creating` (create-only): all scaffolding-name patterns. The
  model may not *invent* new files matching these globs, but may
  freely *edit* a real repo file by the same name if one exists. The
  invariant: "you may not invent new validation scaffolding."
- Patterns derive from three layers: n=75 baseline leakage,
  v1.5-pressure-rerun escapes, and v2-rerun novel shapes. See the
  module docstring above for the empirical history.

## Forbids
- repo_root/**

## Forbids creating
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
- src/test_*.py
- test_encoded_*.py
- **/test_encoded_*.py
- verify_fix.py
- **/verify_fix.py
- verify_*.py
- **/verify_*.py
- *_verify.py
- **/*_verify.py
- tmp_test.py
- tmp_install.py
- **/tmp_*.py
- **/test_*_verify.py
- **/test_*_time.py
- test_*.py
- **/test_*.py
- test_fix_*.py
- **/test_fix_*.py
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
        # Synthetic SpecDD .sdd file may be present in the working tree
        # (Lever 2 fixture-prep injection). It is removed before
        # extract_diff so it does not contaminate predictions.json.
        "--allow-dirty",
    ]
    if config:
        cmd.extend(["--config", str(config)])
    env = os.environ.copy()
    # SWE-bench commits are throwaway scaffolding for diff extraction —
    # luxe maintain's pr.py runs `git commit` to seal the agent's edits
    # so extract_diff can pull a clean patch. Honoring the user's global
    # commit.gpgsign + SSH-key signing config blocks the commit when the
    # key requires an interactive passphrase (observed on n=75 attempt
    # 2026-05-05 — every instance failed with `Load key ... incorrect
    # passphrase`). Override unconditionally for bench runs; nothing is
    # ever pushed.
    env.setdefault("GIT_CONFIG_COUNT", "1")
    env.setdefault("GIT_CONFIG_KEY_0", "commit.gpgsign")
    env.setdefault("GIT_CONFIG_VALUE_0", "false")
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
    write_pressure: bool = True,
    early_bail: bool = True,
    action_density_gate: bool = True,
    convergence_gate: bool = True,
    early_bail_commit_only: bool = False,
    tiered_compact: bool = False,
    tiered_compact_threshold: float | None = None,
) -> SweBenchInvocationResult:
    """End-to-end per-instance run: ensure repo → inject .sdd → invoke luxe → strip .sdd → extract diff.

    `inject_sdd` (default True) drops a synthetic `<repo_basename>.sdd`
    with anti-reproducer Forbids globs at the cloned-repo root before
    the agent runs, and removes it before diff extraction. Set False to
    reproduce the pre-Lever-2 baseline behaviour.

    `write_pressure` (default True) is the paired actuation for the .sdd
    constraint: when True AND inject_sdd is True, sets
    `LUXE_WRITE_PRESSURE=1` in the subprocess env so the mid-loop
    Mode B intervention from v1.4.1 fires when the model reads-many-but-
    writes-zero. The two ship together because constrained execution
    requires enforced actuation; n=75 measured `empty_patch +4` when
    constraint shipped without actuation.

    `early_bail` (default True, v1.9) sets `LUXE_EARLY_BAIL=1` so the
    early-bail intervention fires at step ≥4 with reads ≥4 and zero
    writes. Paired with the soft-anchor message variant below.
    Previously (v1.7 / v1.8) this env was set on the command line; the
    adapter now wires it explicitly so the SWE-bench intervention stack
    is reproducible without external env preconditions.

    `action_density_gate` (default True, v1.9) sets
    `LUXE_ACTION_DENSITY_GATE=1` so the staged-escalation density gate
    fires at step ≥6 with low tool count + high token output, in
    standalone or post_bail_rescue mode. Thresholds derived from
    scripts/mine_action_density.py.

    `convergence_gate` (default True, v1.10) sets
    `LUXE_CONVERGENCE_GATE=1` so the conditional-stacking convergence
    score gates early_bail (suppress on diffuse-recon) and
    action_density_gate (suppress on high convergence), and swaps the
    soft_anchor message for commit_imperative when the model has
    converged on a target. Pure stacking of text-level interventions
    is non-Pareto per v1.9 Phase D evidence; the v1.10 score restores
    Pareto-correctness by making fires context-aware. See
    luxe.agents.convergence for the score primitives.
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
        if write_pressure:
            extra_env = {**(extra_env or {}), "LUXE_WRITE_PRESSURE": "1"}
    # v1.9: switch the early_bail message to the soft-anchor variant for
    # SWE-bench. v1.8's no_abstain text closed the v17 wrong→empty class
    # but introduced confidence-collapse: 2 strong→empty regressions
    # (sphinx-10435, sympy-13031). soft-anchor preserves no-abstain's
    # commitment pressure but adds a selection heuristic ("choose the
    # highest-probability bug location — even if uncertain") so the
    # planner can act under uncertainty rather than stall. maintain_suite
    # still uses the default (where abstain is sometimes legitimate).
    extra_env = {**(extra_env or {}), "LUXE_EARLY_BAIL_MODE": "soft_anchor"}
    if early_bail:
        extra_env = {**(extra_env or {}), "LUXE_EARLY_BAIL": "1"}
    if action_density_gate:
        extra_env = {**(extra_env or {}), "LUXE_ACTION_DENSITY_GATE": "1"}
    if convergence_gate:
        extra_env = {**(extra_env or {}), "LUXE_CONVERGENCE_GATE": "1"}
    # Refined-port ablation (2026-05-26 edit-quality investigation): suppress
    # soft_anchor + breadth_probe variants; commit_imperative (score >= HIGH)
    # still fires. See loop.py around line 532 for the gate.
    if early_bail_commit_only:
        extra_env = {**(extra_env or {}), "LUXE_EARLY_BAIL_COMMIT_ONLY": "1"}
    # forge-hybrid Phase 2 (A): wire TieredCompact when flagged; default OFF.
    # Replaces the elide_old_tool_results call at loop.py's pre-chat
    # compaction site with the 3-phase strategy.
    if tiered_compact:
        extra_env = {**(extra_env or {}), "LUXE_TIERED_COMPACT": "1"}
        if tiered_compact_threshold is not None:
            extra_env = {
                **(extra_env or {}),
                "LUXE_TIERED_COMPACT_THRESHOLD": str(tiered_compact_threshold),
            }
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
