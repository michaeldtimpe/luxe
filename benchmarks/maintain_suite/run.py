"""Acceptance suite runner — drives `luxe maintain` for each fixture.

Usage:
  python -m benchmarks.maintain_suite.run [--all | --id <fixture-id> ...]
                                          [--fixtures path/to/fixtures.yaml]
                                          [--output ./acceptance/]

Per fixture:
  1. Resolve repo_url / repo_path; clone if URL, checkout base_sha.
  2. Verify required_env vars are set; otherwise skip with status=skipped_credentials.
  3. Invoke luxe maintain (in-process via subprocess to keep state isolated).
  4. Read run_dir for citation lint result + PR URL.
  5. Call grade.grade_fixture and persist FixtureResult.
  6. Print a summary table.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from benchmarks.maintain_suite.grade import (
    Fixture,
    FixtureResult,
    grade_fixture,
    summarize,
)


def _load_fixtures(path: Path) -> list[Fixture]:
    raw = yaml.safe_load(path.read_text()) or {}
    return [Fixture.from_dict(d) for d in (raw.get("fixtures") or [])]


def _resolve_repo(fixture: Fixture, work_dir: Path) -> Path | None:
    """Clone (URL) or copy (path); checkout base_sha. Returns local path or None."""
    if fixture.repo_path:
        p = Path(fixture.repo_path).expanduser().resolve()
        if not p.is_dir():
            return None
        if fixture.base_sha:
            subprocess.run(["git", "checkout", "-q", fixture.base_sha], cwd=p,
                           capture_output=True, check=False)
        return p
    if fixture.repo_url:
        target = work_dir / f"{fixture.id}-clone"
        rc = subprocess.run(["git", "clone", "--quiet", fixture.repo_url, str(target)],
                            capture_output=True).returncode
        if rc != 0:
            return None
        if fixture.base_sha:
            subprocess.run(["git", "checkout", "-q", fixture.base_sha], cwd=target,
                           capture_output=True, check=False)
        return target
    return None


def _run_luxe_maintain(repo_path: Path, fixture: Fixture,
                       run_id_capture: list[str]) -> int:
    """Spawn `luxe maintain` for the fixture. Captures the run_id from stdout."""
    cmd = [
        sys.executable, "-m", "luxe.cli", "maintain",
        str(repo_path), fixture.goal,
        "--task", fixture.task_type,
        "--mode", "auto",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    # Try to extract run_id from output (CLI prints "run_id=<id>" early).
    import re
    m = re.search(r"run_id=([0-9a-f]{8,})", proc.stdout + proc.stderr)
    if m:
        run_id_capture.append(m.group(1))
    return proc.returncode


def _read_run_artefacts(run_id: str) -> dict[str, Any]:
    """Look up the run dir to extract pr_state + citation lint info."""
    rd = Path.home() / ".luxe" / "runs" / run_id
    out: dict[str, Any] = {
        "pr_url": "",
        "pr_opened": False,
        "citations_unresolved": 0,
        "citations_total": 0,
    }
    pr_state = rd / "pr_state.json"
    if pr_state.is_file():
        try:
            data = json.loads(pr_state.read_text())
            out["pr_url"] = data.get("pr_url", "")
            out["pr_opened"] = bool(data.get("pr_url"))
        except json.JSONDecodeError:
            pass
    # Citation lint records are written by the orchestrator's events.
    events = rd / "events.jsonl"
    if events.is_file():
        for line in events.read_text().splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("kind") == "citation_lint_blocked":
                out["citations_unresolved"] = int(ev.get("unresolved", 0))
            elif ev.get("kind") == "citation_lint_passed":
                out["citations_total"] = int(ev.get("count", 0))
    return out


def run_one(fixture: Fixture, work_dir: Path) -> FixtureResult:
    # Required env check
    missing = [v for v in fixture.required_env if not os.environ.get(v)]
    if missing:
        return FixtureResult(
            fixture_id=fixture.id, skipped=True,
            skipped_reason=f"missing env: {', '.join(missing)}",
        )

    repo = _resolve_repo(fixture, work_dir)
    if repo is None:
        return FixtureResult(
            fixture_id=fixture.id, error=f"could not resolve repo for {fixture.id}",
        )

    base_sha = fixture.base_sha
    if not base_sha:
        rc, out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                 capture_output=True, text=True).returncode, ""
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                              capture_output=True, text=True)
        base_sha = proc.stdout.strip()

    captured: list[str] = []
    rc = _run_luxe_maintain(repo, fixture, captured)
    if not captured:
        return FixtureResult(
            fixture_id=fixture.id,
            error=f"luxe maintain produced no run_id (rc={rc})",
        )
    run_id = captured[0]
    artefacts = _read_run_artefacts(run_id)

    return grade_fixture(
        fixture, repo,
        pr_url=artefacts["pr_url"],
        pr_opened=artefacts["pr_opened"],
        citations_unresolved=artefacts["citations_unresolved"],
        citations_total=artefacts["citations_total"],
        base_sha=base_sha,
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="luxe acceptance suite")
    parser.add_argument("--fixtures", default=None,
                        help="Path to fixtures.yaml (default: alongside this file)")
    parser.add_argument("--output", default="./acceptance",
                        help="Where to write per-fixture result JSON")
    parser.add_argument("--id", action="append", default=[],
                        help="Run only this fixture id (repeatable)")
    parser.add_argument("--all", action="store_true",
                        help="Run every fixture in the file")
    args = parser.parse_args()

    fixtures_path = Path(args.fixtures) if args.fixtures else \
        Path(__file__).parent / "fixtures.yaml"
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    fixtures = _load_fixtures(fixtures_path)
    if args.id:
        fixtures = [f for f in fixtures if f.id in set(args.id)]
    elif not args.all:
        print("No fixtures selected. Pass --all or --id <id>.")
        return 2
    if not fixtures:
        print("No matching fixtures.")
        return 2

    results: list[FixtureResult] = []
    with tempfile.TemporaryDirectory(prefix="luxe-acceptance-") as td:
        work_dir = Path(td)
        for f in fixtures:
            print(f"\n━━━ {f.id}  [{f.task_type}]  {f.goal}")
            try:
                r = run_one(f, work_dir)
            except Exception as e:
                r = FixtureResult(fixture_id=f.id, error=f"{type(e).__name__}: {e}")
            results.append(r)
            (output / f"{f.id}.json").write_text(json.dumps(r.to_dict(), indent=2))
            verdict = ("PASS" if r.passed else "SKIP" if r.skipped
                       else "ERROR" if r.error else "FAIL")
            print(f"  {verdict}  score={r.score}/{r.max_score}  "
                  f"{r.expected_outcome_detail[:80]}")

    summary = summarize(results)
    print("\n━━━ Summary")
    print(json.dumps(summary, indent=2))
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    return 0 if summary["v1_release_gate"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
