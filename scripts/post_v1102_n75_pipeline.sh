#!/usr/bin/env bash
# v1.10.2 post-n=75 pipeline. Mirrors post_v1101_n75_pipeline.sh
# pointed at v1.10.2 paths and using v1.10.1 (not v1.10) as the
# baseline taxonomy for cross-cycle delta.

set -euo pipefail

CYCLE_DIR="acceptance/swebench/post_specdd_v1102_n75/rep_1"
PREDS="${CYCLE_DIR}/predictions.json"
HARNESS_DIR="${CYCLE_DIR}/harness"
V1102_TAX="acceptance/v1102_taxonomy/v1102_n75_full_stack_swebench.json"
V1101_TAX="acceptance/v1101_taxonomy/v1101_n75_full_stack_swebench.json"

if [ ! -f "${PREDS}" ]; then
    echo "FATAL: ${PREDS} not found — n=75 bench has not completed." >&2
    exit 2
fi

echo "=== [1/4] Save run_id_manifest ==="
python -m scripts.save_run_id_manifest "${PREDS}"

echo "=== [2/4] Generate v1.10.2 taxonomy via compare_v110.py ==="
mkdir -p "$(dirname "${V1102_TAX}")"
python -m scripts.compare_v110 \
    --predictions "${PREDS}" \
    --baseline-taxonomy "${V1101_TAX}" \
    --label "v1.10.2" \
    || true   # compare exits rc=2 on ship-floor miss; we want the report regardless

# Also write a tracked v1.10.2 taxonomy file (compare_v110.py only prints
# stdout report; we need the JSON for the analyzer + backfill consumers).
python -c "
import json
from pathlib import Path
from scripts.compare_v110 import classify_arm, annotate_patch_len_deltas
target = classify_arm(Path('${PREDS}'))
baseline_rows = {r['instance_id']: r for r in json.loads(Path('${V1101_TAX}').read_text())['rows']}
annotate_patch_len_deltas(target, baseline_rows)
rows = sorted(({'instance_id': iid, **d} for iid, d in target.items()),
              key=lambda r: r['instance_id'])
Path('${V1102_TAX}').write_text(json.dumps({'rows': rows}, indent=2))
print(f'wrote v1102 taxonomy: {len(rows)} rows')
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
    run_id='luxe_v1102_n75',
)
write_harness_summary(results, out / 'harness_summary.json')
print('Docker harness complete:', len(results), 'instances')
"

echo "=== [4/4] Cross-cycle analysis (v1.10.2 vs v1.10.1 baseline) ==="
python -c "
import json, sys
from pathlib import Path
sys.path.insert(0, '.')
import scripts.analyze_v110_harness as A

A.V110_HARNESS = Path('${HARNESS_DIR}/harness_summary.json')
A.V110_TAX = Path('${V1102_TAX}')
A.V19_HARNESS = Path('acceptance/swebench/post_specdd_v1101_n75/rep_1/harness/harness_summary.json')
A.V19_TAX = Path('${V1101_TAX}')
# v1.10.2 is observability-only; matplotlib-14623 recovery should be
# preserved (or wash within variance).
A.RECOVERIES = []  # no specific recovery thesis for v1.10.2
A.main()
" 2>&1 || true

echo ""
echo "=== Pipeline complete. Review:"
echo "  v1.10.2 taxonomy:  ${V1102_TAX}"
echo "  Docker summary:    ${HARNESS_DIR}/harness_summary.json"
echo "  Run id manifest:   ${CYCLE_DIR}/run_id_manifest.json"
