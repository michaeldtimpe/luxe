#!/usr/bin/env python
"""Re-grade Phase 2 results with the strict gates added to grade.py.

Walks acceptance/<output>/<variant>/<fixture>/result.json, fetches each PR
from GitHub via `gh pr view --json files` and `gh pr diff`, then re-runs
gating logic. If any gate triggers (destructive_diff, role_name_leak,
placeholder_diff), the result is downgraded:
  - score → 0
  - error → "<gate_name>: <detail>"
  - passed → False (since score < threshold AND error is set)

Writes result_v2.json next to the original result.json (non-destructive)
and emits regrade_comparison.json showing v1 vs v2 standings.

Usage:
  python scripts/regrade_phase2.py
  python scripts/regrade_phase2.py --output acceptance/phase2
  python scripts/regrade_phase2.py --output acceptance/phase1   # also works on Phase 1
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from benchmarks.maintain_suite.grade import (  # noqa: E402
    FixtureResult,
    apply_strict_gates,
)


_PR_URL_RX = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")


def parse_pr_url(url: str) -> tuple[str, str, int] | None:
    m = _PR_URL_RX.match(url or "")
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def fetch_pr_files(owner: str, repo: str, num: int) -> list[dict]:
    """Returns [{path, additions, deletions}] for each file in the PR."""
    cmd = ["gh", "pr", "view", str(num), "--repo", f"{owner}/{repo}",
           "--json", "files"]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return []
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return d.get("files") or []


def fetch_pr_diff_added_lines(owner: str, repo: str, num: int) -> str:
    """Returns concatenated added-line text from the PR diff (lines starting
    with '+', excluding the '+++' file headers)."""
    cmd = ["gh", "pr", "diff", str(num), "--repo", f"{owner}/{repo}"]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return ""
    added: list[str] = []
    for line in r.stdout.splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
    return "\n".join(added)


def regrade_one(result_path: Path, task_type_by_fixture: dict[str, str]) -> dict:
    """Re-grade one fixture; write result_v2.json; return regrade summary."""
    raw = json.loads(result_path.read_text())
    fr = FixtureResult(**{k: v for k, v in raw.items()
                          if k in FixtureResult.__dataclass_fields__})
    fixture_id = fr.fixture_id
    task_type = task_type_by_fixture.get(fixture_id, "")

    pr_info = parse_pr_url(fr.pr_url)
    if pr_info is None:
        # No PR URL = nothing to re-grade. Pass through unchanged.
        v2_path = result_path.with_name("result_v2.json")
        v2_path.write_text(json.dumps(asdict(fr), indent=2))
        return {
            "fixture_id": fixture_id,
            "v1_passed": fr.passed, "v1_score": fr.score,
            "v2_passed": fr.passed, "v2_score": fr.score,
            "gates_triggered": [],
            "note": "no PR URL — passthrough",
        }

    owner, repo, num = pr_info
    files = fetch_pr_files(owner, repo, num)
    paths = [f["path"] for f in files]
    additions = sum(int(f.get("additions", 0)) for f in files)
    deletions = sum(int(f.get("deletions", 0)) for f in files)
    added_text = fetch_pr_diff_added_lines(owner, repo, num)

    triggered = apply_strict_gates(
        task_type=task_type,
        file_paths=paths,
        additions=additions,
        deletions=deletions,
        diff_added_text=added_text,
    )

    v2 = FixtureResult(**asdict(fr))
    v2.diff_additions = additions
    v2.diff_deletions = deletions
    v2.gates_triggered = [{"gate": g, "detail": d} for g, d in triggered]

    if triggered:
        gate_names = ",".join(g for g, _ in triggered)
        v2.error = f"{gate_names}: " + "; ".join(d for _, d in triggered)
        v2.score = 0  # Strict mode: no credit for any criterion if a gate fires.

    v2_path = result_path.with_name("result_v2.json")
    v2_path.write_text(json.dumps(asdict(v2), indent=2))
    return {
        "fixture_id": fixture_id,
        "v1_passed": fr.passed, "v1_score": fr.score,
        "v2_passed": v2.passed, "v2_score": v2.score,
        "gates_triggered": [g for g, _ in triggered],
        "additions": additions, "deletions": deletions,
    }


def load_fixture_task_types() -> dict[str, str]:
    """Read fixtures.yaml so we know which task_type each fixture has —
    needed because the gates only fire on write-mode tasks."""
    import yaml
    p = ROOT / "benchmarks" / "maintain_suite" / "fixtures.yaml"
    raw = yaml.safe_load(p.read_text()) or {}
    return {f["id"]: f.get("task_type", "")
            for f in (raw.get("fixtures") or [])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", type=Path, default=ROOT / "acceptance" / "phase2",
                    help="Maintain-suite output dir to re-grade")
    ap.add_argument("--variant", action="append", default=[],
                    help="Restrict to specific variant_ids (repeatable)")
    args = ap.parse_args()

    if not args.output.is_dir():
        print(f"[error] {args.output} not a directory", file=sys.stderr)
        return 2

    task_types = load_fixture_task_types()

    # Two layouts to handle:
    #   <output>/<variant>/<fixture>/result.json   (multi-variant runs)
    #   <output>/<fixture>/result.json             (single-variant runs)
    rows: list[dict] = []
    for result_path in sorted(args.output.rglob("result.json")):
        rel = result_path.relative_to(args.output)
        parts = rel.parts
        if len(parts) == 3:        # variant/fixture/result.json
            variant_id, fixture_id, _ = parts
        elif len(parts) == 2:      # fixture/result.json
            variant_id, fixture_id = "(default)", parts[0]
        else:
            continue
        if args.variant and variant_id not in args.variant:
            continue
        print(f"  re-grading {variant_id}/{fixture_id} ...", flush=True)
        row = regrade_one(result_path, task_types)
        row["variant_id"] = variant_id
        rows.append(row)

    # Per-variant before/after table.
    by_variant: dict[str, list[dict]] = {}
    for r in rows:
        by_variant.setdefault(r["variant_id"], []).append(r)

    print(f"\n━━━ Strict-regrade comparison")
    header = (f"  {'variant':30s}  {'v1 pass':>7}  {'v2 pass':>7}  "
              f"{'flagged':>7}  {'gates':<60}")
    print(header)
    print(f"  {'-' * (len(header) - 2)}")
    summary: dict[str, dict] = {}
    for vid in sorted(by_variant):
        rs = by_variant[vid]
        v1p = sum(1 for r in rs if r["v1_passed"])
        v2p = sum(1 for r in rs if r["v2_passed"])
        flagged = sum(1 for r in rs if r["gates_triggered"])
        gates_seen: dict[str, int] = {}
        for r in rs:
            for g in r["gates_triggered"]:
                gates_seen[g] = gates_seen.get(g, 0) + 1
        gates_summary = ", ".join(f"{g}×{n}" for g, n in sorted(gates_seen.items())) or "—"
        print(f"  {vid:30s}  {v1p:>7}  {v2p:>7}  {flagged:>7}  {gates_summary:<60}")
        summary[vid] = {
            "fixtures": len(rs),
            "v1_passed": v1p, "v2_passed": v2p,
            "flagged": flagged,
            "gates_seen": gates_seen,
            "fixtures_detail": rs,
        }

    out_path = args.output / "regrade_comparison.json"
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"\n  Saved: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
