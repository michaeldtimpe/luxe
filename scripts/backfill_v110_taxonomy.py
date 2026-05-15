"""v1.10.2 — regenerate v1.10 and v1.10.1 taxonomies with the new
CONFIDENCE_COLLAPSE_* split + write-locus + recent_path_diversity fields.

Without this backfill, charts comparing v1.10 / v1.10.1 / v1.10.2 show
a volatile break in the time series rather than a refinement (the same
trajectories would classify under different failure-class enum members
depending on which version generated the taxonomy). The backfill
re-classifies historical predictions against the v1.10.2 classifier so
all three taxonomies use the same enum.

Inputs (existing on disk; gitignored):
  acceptance/swebench/post_specdd_v110_n75/rep_1/predictions.json
  acceptance/swebench/post_specdd_v110_n75/rep_1/run_id_manifest.json
  acceptance/swebench/post_specdd_v1101_n75/rep_1/predictions.json
  acceptance/swebench/post_specdd_v1101_n75/rep_1/run_id_manifest.json

Outputs (overwrites):
  acceptance/v110_taxonomy/v110_n75_full_stack_swebench.json
  acceptance/v1101_taxonomy/v1101_n75_full_stack_swebench.json

Reuses scripts/compare_v110.classify_arm; this script is just an
orchestration shell. Idempotent and re-runnable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.compare_v110 import classify_arm, annotate_patch_len_deltas


CYCLES = [
    {
        "label": "v1.10",
        "preds": Path("acceptance/swebench/post_specdd_v110_n75/rep_1/predictions.json"),
        "taxonomy_out": Path("acceptance/v110_taxonomy/v110_n75_full_stack_swebench.json"),
        "baseline_taxonomy": None,  # v1.10 has no baseline
    },
    {
        "label": "v1.10.1",
        "preds": Path("acceptance/swebench/post_specdd_v1101_n75/rep_1/predictions.json"),
        "taxonomy_out": Path("acceptance/v1101_taxonomy/v1101_n75_full_stack_swebench.json"),
        "baseline_taxonomy": Path("acceptance/v110_taxonomy/v110_n75_full_stack_swebench.json"),
    },
]


def write_taxonomy(rows_by_iid: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(({"instance_id": iid, **d} for iid, d in rows_by_iid.items()),
                  key=lambda r: r["instance_id"])
    out.write_text(json.dumps({"rows": rows}, indent=2))


def main() -> int:
    for cycle in CYCLES:
        label = cycle["label"]
        preds = cycle["preds"]
        out = cycle["taxonomy_out"]
        if not preds.is_file():
            print(f"  ! {label} predictions missing: {preds}", file=sys.stderr)
            continue
        print(f"=== Backfilling {label} taxonomy → {out} ===")
        target = classify_arm(preds)
        # Annotate patch_len_delta when a baseline taxonomy exists
        baseline_path = cycle.get("baseline_taxonomy")
        if baseline_path and baseline_path.is_file():
            baseline_rows = {
                r["instance_id"]: r
                for r in json.loads(baseline_path.read_text())["rows"]
            }
            annotate_patch_len_deltas(target, baseline_rows)
        write_taxonomy(target, out)
        # Summary: count classes
        from collections import Counter
        chain_counter: Counter[str] = Counter()
        for d in target.values():
            for c in d.get("failure_chain") or []:
                chain_counter[c] += 1
        print(f"  {label} written ({len(target)} rows). Failure-class counts:")
        for c, n in chain_counter.most_common():
            print(f"    {c:<45} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
