#!/usr/bin/env bash
# v1.11 post-n=75 pipeline. Parameterized by REP (default rep_1).
# Mirrors post_v1105_n75_pipeline.sh; v1.11 target, v1.10.5 baseline.
#
# Usage: scripts/post_v111_n75_pipeline.sh rep_2

set -euo pipefail

REP="${1:-rep_1}"

CYCLE_DIR="acceptance/swebench/post_v111_n75/${REP}"
PREDS="${CYCLE_DIR}/predictions.json"
HARNESS_DIR="${CYCLE_DIR}/harness"

V111_TAX="acceptance/v111_taxonomy/v111_n75_${REP}_full_stack_swebench.json"
if [ "${REP}" = "rep_1" ]; then
    V111_TAX="acceptance/v111_taxonomy/v111_n75_full_stack_swebench.json"
    V1105_TAX="acceptance/v1105_taxonomy/v1105_n75_full_stack_swebench.json"
else
    V1105_TAX="acceptance/v1105_taxonomy/v1105_n75_${REP}_full_stack_swebench.json"
fi

if [ ! -f "${PREDS}" ]; then
    echo "FATAL: ${PREDS} not found — n=75 bench has not completed." >&2
    exit 2
fi

echo "=== [1/4] Save run_id_manifest (${REP}) ==="
python -m scripts.save_run_id_manifest "${PREDS}"

echo "=== [2/4] Generate v1.11 taxonomy (baseline v1.10.5 ${REP}) ==="
mkdir -p "$(dirname "${V111_TAX}")"
python -c "
import json
from pathlib import Path
from scripts.compare_v110 import classify_arm, annotate_patch_len_deltas
target = classify_arm(Path('${PREDS}'))
baseline_rows = {r['instance_id']: r for r in json.loads(Path('${V1105_TAX}').read_text())['rows']}
annotate_patch_len_deltas(target, baseline_rows)
rows = sorted(({'instance_id': iid, **d} for iid, d in target.items()),
              key=lambda r: r['instance_id'])
Path('${V111_TAX}').write_text(json.dumps({'rows': rows}, indent=2))
print(f'wrote v111 taxonomy (${REP}): {len(rows)} rows')
"

echo "=== [3/4] Docker harness (~35m wall) ==="
mkdir -p "${HARNESS_DIR}"
python -c "
from pathlib import Path
from benchmarks.swebench.harness import run_harness, write_harness_summary
out = Path('${HARNESS_DIR}')
results = run_harness(
    predictions_path=Path('${PREDS}'),
    output_dir=out,
    run_id='luxe_v111_n75_${REP}',
)
write_harness_summary(results, out / 'harness_summary.json')
print('Docker harness complete:', len(results), 'instances')
"

echo "=== [4/4] Cross-cycle analysis (v1.11 ${REP} vs v1.10.5 baseline) ==="
python -c "
import sys
from pathlib import Path
sys.path.insert(0, '.')
import scripts.analyze_v110_harness as A
A.V110_HARNESS = Path('${HARNESS_DIR}/harness_summary.json')
A.V110_TAX = Path('${V111_TAX}')
A.V19_HARNESS = Path('acceptance/swebench/post_specdd_v1105_n75/rep_1/harness/harness_summary.json')
A.V19_TAX = Path('acceptance/v1105_taxonomy/v1105_n75_full_stack_swebench.json')
A.RECOVERIES = []
A.main()
" 2>&1 || true

echo ""
echo "=== Pipeline complete (${REP}). Review:"
echo "  v1.11 taxonomy:  ${V111_TAX}"
echo "  Docker summary:  ${HARNESS_DIR}/harness_summary.json"
echo "  Run id manifest: ${CYCLE_DIR}/run_id_manifest.json"
