#!/usr/bin/env python
"""Re-grade an acceptance run against the *current* grader without re-running luxe.

The agent loop is what's slow (60-120 min per 10-fixture sweep); the grader
itself takes seconds. Every fixture's pushed branch lives in the local cache
at the path `Fixture.repo_url` points to, so we can:

    1. Read each fixture's <output>/<variant>/<fixture>/state.json for run_id.
    2. Read ~/.luxe/runs/<run_id>/pr_state.json for branch_name.
    3. Clone the cache repo to /tmp (local clone — sub-second).
    4. Checkout origin/<branch_name>.
    5. Call grade_fixture() directly with that worktree.
    6. Write result_regraded.json next to the original.

Sister of scripts/regrade_phase2.py (which fetches PR state from GitHub via
gh). This one works with the offline-cache fixture setup introduced
2026-05-01: every fixture's repo_url is a local path, every push lands in
that local repo's branch list.

Usage:
  python scripts/regrade_local.py --output acceptance/v1_temp0_probe_b
  python scripts/regrade_local.py --output acceptance/v1_temp0_probe_b --id <fixture>
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from benchmarks.maintain_suite.grade import (  # noqa: E402
    Fixture,
    FixtureResult,
    grade_fixture,
)


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True, check=False)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def load_fixtures() -> dict[str, Fixture]:
    """Read fixtures.yaml; return {fixture_id: Fixture}."""
    import yaml
    p = ROOT / "benchmarks" / "maintain_suite" / "fixtures.yaml"
    raw = yaml.safe_load(p.read_text()) or {}
    return {f["id"]: Fixture.from_dict(f) for f in (raw.get("fixtures") or [])}


def _pushed_branch_for(run_id: str) -> str:
    """Look up the branch the agent loop pushed for a given luxe_run_id.
    Empty string means no branch was pushed (e.g. stuck-loop run)."""
    p = Path.home() / ".luxe" / "runs" / run_id / "pr_state.json"
    if not p.is_file():
        return ""
    try:
        d = json.loads(p.read_text())
    except json.JSONDecodeError:
        return ""
    return str(d.get("branch_name", ""))


def _prepare_worktree(fixture: Fixture, branch: str, dest: Path) -> bool:
    """Local-clone the fixture's repo to `dest` and check out the agent's
    pushed branch. Returns True if the branch was checked out, False if no
    branch existed (so the worktree is at default HEAD == base_sha territory).
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    rc, out = _run(["git", "clone", "--quiet", "--local", fixture.repo_url, str(dest)])
    if rc != 0:
        raise RuntimeError(f"clone failed for {fixture.id}: {out}")
    if not branch:
        # No pushed branch (stuck-loop run); leave HEAD on the default branch
        # but reset to base_sha so the grader sees zero diff vs base_sha.
        if fixture.base_sha:
            _run(["git", "checkout", "-q", fixture.base_sha], cwd=dest)
        return False
    rc, out = _run(["git", "checkout", "-q", f"origin/{branch}"], cwd=dest)
    if rc != 0:
        # Branch reference doesn't exist in cache (rare). Fall back to base_sha.
        if fixture.base_sha:
            _run(["git", "checkout", "-q", fixture.base_sha], cwd=dest)
        return False
    return True


def regrade_one(result_path: Path, fixtures: dict[str, Fixture]) -> dict:
    """Re-grade one fixture against the current grader. Writes
    result_regraded.json next to result_path. Returns a row summary."""
    raw = json.loads(result_path.read_text())
    fixture_id = raw.get("fixture_id", "")
    if fixture_id not in fixtures:
        return {"fixture_id": fixture_id, "skipped": "not in fixtures.yaml"}

    fixture = fixtures[fixture_id]
    state_path = result_path.with_name("state.json")
    state = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            pass
    run_id = str(state.get("luxe_run_id", ""))
    branch = _pushed_branch_for(run_id) if run_id else ""

    # Carry over agent-loop outputs we don't recompute: pr_url, pr_opened,
    # citation counts. (The grader's responsibility is the *grading* logic;
    # the agent-loop's responsibility was the diff and PR cycle, which we're
    # treating as ground truth from the original run.)
    pr_url = str(raw.get("pr_url", ""))
    pr_opened = bool(raw.get("pr_opened", False))
    cit_unres = int(raw.get("citations_unresolved", 0))
    cit_total = int(raw.get("citations_total", 0))

    worktree = Path("/tmp") / f"regrade-{fixture_id}"
    _prepare_worktree(fixture, branch, worktree)

    new_result = grade_fixture(
        fixture, worktree,
        pr_url=pr_url,
        pr_opened=pr_opened,
        citations_unresolved=cit_unres,
        citations_total=cit_total,
        base_sha=fixture.base_sha,
    )

    out_path = result_path.with_name("result_regraded.json")
    out_path.write_text(json.dumps(asdict(new_result), indent=2))

    return {
        "fixture_id": fixture_id,
        "v1_passed": bool(raw.get("score", 0) >= 4 and not raw.get("error", "")
                          and not raw.get("skipped", False)),
        "v1_score": int(raw.get("score", 0)),
        "v2_passed": new_result.passed,
        "v2_score": new_result.score,
        "v1_outcome_passed": raw.get("expected_outcome_passed"),
        "v2_outcome_passed": new_result.expected_outcome_passed,
        "v1_outcome_detail": str(raw.get("expected_outcome_detail", ""))[:120],
        "v2_outcome_detail": new_result.expected_outcome_detail[:120],
        "v1_diff_add": int(raw.get("diff_additions", 0)),
        "v1_diff_del": int(raw.get("diff_deletions", 0)),
        "v2_diff_add": new_result.diff_additions,
        "v2_diff_del": new_result.diff_deletions,
        "v1_gates": [g.get("name", g.get("gate", "")) for g in raw.get("gates_triggered", [])],
        "v2_gates": [g.get("name", g.get("gate", "")) for g in new_result.gates_triggered],
        "branch": branch,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", type=Path, required=True,
                    help="acceptance/<run> dir to re-grade")
    ap.add_argument("--id", action="append", default=[],
                    help="Restrict to specific fixture_id(s) (repeatable)")
    args = ap.parse_args()

    if not args.output.is_dir():
        print(f"[error] {args.output} not a directory", file=sys.stderr)
        return 2

    fixtures = load_fixtures()
    rows: list[dict] = []
    for result_path in sorted(args.output.rglob("result.json")):
        fixture_id = result_path.parent.name
        if args.id and fixture_id not in args.id:
            continue
        print(f"  re-grading {fixture_id} ...", flush=True)
        rows.append(regrade_one(result_path, fixtures))

    if not rows:
        print("[warn] no fixtures re-graded", file=sys.stderr)
        return 1

    # Comparison table.
    print(f"\n━━━ Re-grade comparison (v1 = stored result.json, v2 = current grader)")
    header = (f"  {'fixture':45s}  {'v1':>5}  {'v2':>5}  "
              f"{'add Δ':>10}  {'del Δ':>10}  {'gates Δ':<35}")
    print(header)
    print(f"  {'-' * (len(header) - 2)}")
    for r in rows:
        if r.get("skipped"):
            print(f"  {r['fixture_id']:45s}  SKIP   {r['skipped']}")
            continue
        v1 = f"{r['v1_score']}{'P' if r['v1_passed'] else 'F'}"
        v2 = f"{r['v2_score']}{'P' if r['v2_passed'] else 'F'}"
        add_d = f"{r['v1_diff_add']}→{r['v2_diff_add']}"
        del_d = f"{r['v1_diff_del']}→{r['v2_diff_del']}"
        gates_v1 = ",".join(r["v1_gates"]) or "—"
        gates_v2 = ",".join(r["v2_gates"]) or "—"
        gates_d = f"{gates_v1} → {gates_v2}"
        print(f"  {r['fixture_id']:45s}  {v1:>5}  {v2:>5}  "
              f"{add_d:>10}  {del_d:>10}  {gates_d:<35}")

    real_rows = [r for r in rows if not r.get("skipped")]
    v1p = sum(1 for r in real_rows if r["v1_passed"])
    v2p = sum(1 for r in real_rows if r["v2_passed"])
    print(f"\n  totals: v1 PASS = {v1p}/{len(real_rows)}, v2 PASS = {v2p}/{len(real_rows)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
