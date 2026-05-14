#!/usr/bin/env python3
"""Save a `run_id_manifest.json` next to a bench predictions.json so
taxonomy classification stays correct after the workspace stdout.log
gets overwritten by a subsequent run.

The workspace at ~/.luxe/swebench-workspace/<instance>/log/stdout.log
contains the per-instance run_id but only for the MOST RECENT run.
Running v1.9 then v1.10 against the same instance overwrites the
stdout.log so v1.9 run_ids become unrecoverable. Saving a manifest
immediately after each bench preserves the mapping forever.

Usage (after a bench completes):
    python -m scripts.save_run_id_manifest \\
        acceptance/swebench/post_specdd_v110_n75/rep_1/predictions.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

WORKSPACE = Path.home() / ".luxe" / "swebench-workspace"


def run_id_for(instance_id: str) -> str | None:
    log = WORKSPACE / instance_id / "log" / "stdout.log"
    if not log.is_file():
        return None
    rid = None
    for line in log.read_text().splitlines():
        if line.startswith("luxe maintain  run_id="):
            rid = line.split("=", 1)[1].strip()
    return rid


def build_manifest(preds_path: Path) -> dict:
    preds = json.loads(preds_path.read_text())
    out = {}
    missing = 0
    for p in preds:
        inst = p["instance_id"]
        rid = run_id_for(inst)
        out[inst] = {
            "run_id": rid,
            "patch_len": len(p.get("model_patch") or ""),
        }
        if rid is None:
            missing += 1
    return out, missing


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <predictions.json>", file=sys.stderr)
        return 2
    preds_path = Path(argv[1])
    if not preds_path.is_file():
        print(f"  ! missing: {preds_path}", file=sys.stderr)
        return 1
    manifest, missing = build_manifest(preds_path)
    out = preds_path.parent / "run_id_manifest.json"
    out.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {out} — {len(manifest)} entries ({missing} missing run_id)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
