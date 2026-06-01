#!/usr/bin/env bash
# Run all four forge-hybrid ablation cells sequentially:
#   baseline → tiered → respond → trajectory
#
# Resume-safe: each cell's output dir caches per-item progress, so a re-run
# after interruption skips completed work. Crash-recovery friendly.
#
# Usage (from luxe repo root):
#   bash scripts/ablation_run_all.sh [options-passed-through-to-each-cell]
#
# All options are passed through to ablation_run_cell.sh, so:
#   bash scripts/ablation_run_all.sh --bfcl-limit 100 --swebench-smoke 25 \
#       --harness-workers 4
# runs a smaller-scope ablation across all four cells.

set -uo pipefail  # NOTE: no -e — a single cell failure shouldn't halt the
                  # whole matrix. We want partial data over no data.

cd "$(dirname "$0")/.."

CELLS=(baseline tiered respond trajectory)
PASS_THROUGH=("$@")

echo "=== Ablation matrix: ${CELLS[*]} ==="
echo "    pass-through args: ${PASS_THROUGH[*]:-<none>}"
echo "    timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

for cell in "${CELLS[@]}"; do
  echo "===================================================="
  echo "  Starting cell: ${cell}"
  echo "===================================================="
  bash scripts/ablation_run_cell.sh "$cell" "${PASS_THROUGH[@]}"
  rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "  WARNING: cell ${cell} exited rc=${rc} — continuing to next cell"
  fi
  echo ""
done

echo "=== Ablation matrix DONE ==="
echo "    finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""
echo "    next: python scripts/ablation_aggregate.py --root acceptance/agentic_ablation/"
