"""SWE-bench Docker harness wrapper.

PRELIMINARY (2026-05-03). Thin shell around the official `swebench`
package's `run_evaluation`. Requires Docker Desktop (decision point #1 in
the plan); does not run automatically until invoked.

Workflow:
1. Take a `predictions.json` produced by run.py (preds-only mode).
2. Resolve the corresponding instances from the SWE-bench Verified
   dataset.
3. Spawn `swebench.harness.run_evaluation.run_instances(...)` which:
   - Pulls per-instance Docker env images from `swebench` Docker Hub
   - Applies the agent's patch on top of base_commit
   - Runs FAIL_TO_PASS / PASS_TO_PASS test arrays
   - Emits `<run_id>.<model_name>.json` with per-instance `resolved`
4. Aggregate results, write a luxe-format `harness_summary.json`.

Apple Silicon caveat: SWE-bench env images are amd64, so they run under
Rosetta. Adds ~30% overhead and rare flakes on numpy/scipy native-
extension instances. If >5% of n=75 are harness-flaky, switch to a
remote Linux box (run preds-only locally, ship predictions.json over).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class HarnessResult:
    instance_id: str
    resolved: bool = False
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def load_predictions(path: Path) -> dict[str, dict[str, Any]]:
    """Load predictions.json (list-of-rows) and convert to swebench's
    expected `{instance_id: row}` shape.
    """
    rows = json.loads(Path(path).read_text())
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out[row["instance_id"]] = row
    return out


def run_harness(
    predictions_path: Path,
    *,
    output_dir: Path,
    dataset_name: str = "princeton-nlp/SWE-bench_Verified",
    split: str = "test",
    run_id: str | None = None,
    max_workers: int = 2,
    timeout_per_instance_s: int = 1800,
    cache_level: str = "env",
) -> dict[str, HarnessResult]:
    """Run the SWE-bench harness on a predictions.json. Returns
    `{instance_id: HarnessResult}`.

    `cache_level` one of: "none" / "base" / "env" / "instance" — controls
    how aggressively to cache Docker layers. Default "env" balances disk
    use and rebuild speed.
    """
    from swebench.harness.run_evaluation import get_dataset_from_preds, run_instances

    predictions = load_predictions(predictions_path)
    if not run_id:
        run_id = f"luxe_{int(time.time())}"

    instance_ids = list(predictions.keys())
    instances = get_dataset_from_preds(
        dataset_name=dataset_name,
        split=split,
        instance_ids=instance_ids,
        predictions=predictions,
        run_id=run_id,
        rewrite_reports=False,
        exclude_completed=False,
    )

    output_dir.mkdir(parents=True, exist_ok=True)

    run_instances(
        predictions=predictions,
        instances=instances,
        cache_level=cache_level,
        clean=False,
        force_rebuild=False,
        max_workers=max_workers,
        run_id=run_id,
        timeout=timeout_per_instance_s,
    )

    # The harness writes per-instance reports under the cwd by default;
    # collect them into our output_dir for downstream aggregation.
    return collect_results(run_id, output_dir)


def collect_results(run_id: str, output_dir: Path) -> dict[str, HarnessResult]:
    """Walk the harness's output and build {instance_id: HarnessResult}.

    The official harness writes a top-level `<run_id>.<model_name>.json`
    plus per-instance log dirs. We only need the top-level summary for
    pass/fail; the log dirs are kept for hand-debugging.
    """
    from glob import glob
    out: dict[str, HarnessResult] = {}
    summary_files = list(Path.cwd().glob(f"{run_id}.*.json")) + list(
        output_dir.glob(f"{run_id}.*.json")
    )
    for path in summary_files:
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        # Format: {"resolved_ids": [...], "completed_ids": [...], ...}
        # OR per-instance details under "tests". Tolerate both.
        resolved = set(data.get("resolved_ids", []))
        for iid in data.get("completed_ids", resolved):
            out[iid] = HarnessResult(instance_id=iid, resolved=iid in resolved)
        for iid in data.get("error_ids", []):
            out[iid] = HarnessResult(
                instance_id=iid, resolved=False, error="harness_error",
            )
    return out


def write_harness_summary(
    results: dict[str, HarnessResult],
    output_path: Path,
) -> None:
    """Emit harness_summary.json — luxe-format aggregated view."""
    n = len(results)
    n_resolved = sum(1 for r in results.values() if r.resolved)
    payload = {
        "n": n,
        "n_resolved": n_resolved,
        "resolution_rate": (n_resolved / n) if n else 0.0,
        "instances": {
            iid: {"resolved": r.resolved, "error": r.error}
            for iid, r in sorted(results.items())
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2))
