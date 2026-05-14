#!/usr/bin/env bash
# v1.10.1 post-n=75 pipeline. Run from luxe repo root once
# `python -m benchmarks.swebench.run` (n=75) completes successfully.
#
# Steps (each idempotent / re-runnable):
#   1. Save run_id_manifest (preserves workspace state before any later
#      run overwrites stdout.log — non-optional per the v1.10 audit).
#   2. Run compare_v110.py with --baseline-taxonomy pointed at v1.10's
#      taxonomy. Writes the v1.10.1 taxonomy (patch_len_delta +
#      first_correct_file_touch fields included).
#   3. Run the Docker harness against the v1.10.1 predictions.
#   4. Run analyze_v110_harness.py (now points at v1.10.1 harness
#      output) to produce the cross-cycle Docker comparison + silent
#      demotion + locus cross-tab.
#
# All artifacts under acceptance/swebench/post_specdd_v1101_n75/rep_1/.
# Manual review still required before tagging v1.10.1.

set -euo pipefail

CYCLE_DIR="acceptance/swebench/post_specdd_v1101_n75/rep_1"
PREDS="${CYCLE_DIR}/predictions.json"
HARNESS_DIR="${CYCLE_DIR}/harness"
V1101_TAX="acceptance/v1101_taxonomy/v1101_n75_full_stack_swebench.json"
V110_TAX="acceptance/v110_taxonomy/v110_n75_full_stack_swebench.json"

if [ ! -f "${PREDS}" ]; then
    echo "FATAL: ${PREDS} not found — n=75 bench has not completed." >&2
    exit 2
fi

echo "=== [1/4] Save run_id_manifest ==="
python -m scripts.save_run_id_manifest "${PREDS}"

echo "=== [2/4] Generate v1.10.1 taxonomy via compare_v110.py ==="
mkdir -p "$(dirname "${V1101_TAX}")"
python -m scripts.compare_v110 \
    --predictions "${PREDS}" \
    --baseline-taxonomy "${V110_TAX}" \
    --label "v1.10.1" \
    || true   # compare exits rc=2 on ship-floor miss; we want the report regardless

echo "=== [3/4] Docker harness (~35m wall) ==="
mkdir -p "${HARNESS_DIR}"
python -c "
from pathlib import Path
from benchmarks.swebench.harness import run_harness, write_harness_summary
out = Path('${HARNESS_DIR}')
results = run_harness(
    predictions_path=Path('${PREDS}'),
    output_dir=out,
    run_id='luxe_v1101_n75',
)
write_harness_summary(results, out / 'harness_summary.json')
print('Docker harness complete:', len(results), 'instances')
"

echo "=== [4/4] Cross-cycle analysis (analyze_v110_harness.py) ==="
# Re-point the analyzer at v1.10.1 paths via env override (script reads
# v110_n75 paths by default; we want v1101_n75 here).
# Simpler approach: write a tiny adapter that calls the analyzer module.
python -c "
import json, sys
from pathlib import Path
sys.path.insert(0, '.')
# Patch the analyzer's path constants for v1.10.1
import scripts.analyze_v110_harness as A
A.V110_HARNESS = Path('${HARNESS_DIR}/harness_summary.json')
A.V110_TAX = Path('${V1101_TAX}')
A.RECOVERIES = []  # v1.10 recoveries don't apply to v1.10.1; skip thesis B
A.main()
" 2>&1 || true

echo ""
echo "=== Pipeline complete. Review:"
echo "  v1.10.1 taxonomy:  ${V1101_TAX}"
echo "  Docker summary:    ${HARNESS_DIR}/harness_summary.json"
echo "  Run id manifest:   ${CYCLE_DIR}/run_id_manifest.json"
