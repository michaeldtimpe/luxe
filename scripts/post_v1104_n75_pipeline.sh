#!/usr/bin/env bash
# v1.10.4 post-n=75 pipeline. Parameterized by REP (default rep_1).
# Mirrors post_v1103_n75_pipeline.sh, points at v1.10.4 paths, uses
# v1.10.3 (rep_1) as the baseline taxonomy for cross-cycle delta.
#
# Usage:
#   scripts/post_v1104_n75_pipeline.sh rep_2
#   scripts/post_v1104_n75_pipeline.sh rep_3

set -euo pipefail

REP="${1:-rep_1}"

CYCLE_DIR="acceptance/swebench/post_specdd_v1104_n75/${REP}"
PREDS="${CYCLE_DIR}/predictions.json"
HARNESS_DIR="${CYCLE_DIR}/harness"

# Taxonomy file naming follows v1.10.3 convention:
#   rep_1 -> v1104_n75_full_stack_swebench.json
#   rep_N -> v1104_n75_rep_N_full_stack_swebench.json
if [ "${REP}" = "rep_1" ]; then
    V1104_TAX="acceptance/v1104_taxonomy/v1104_n75_full_stack_swebench.json"
else
    V1104_TAX="acceptance/v1104_taxonomy/v1104_n75_${REP}_full_stack_swebench.json"
fi
V1103_TAX="acceptance/v1103_taxonomy/v1103_n75_full_stack_swebench.json"

# Compute v1.10.3 rep_1 taxonomy on the fly if not previously persisted
if [ ! -f "${V1103_TAX}" ]; then
    mkdir -p "$(dirname "${V1103_TAX}")"
    python -c "
import json
from pathlib import Path
from scripts.compare_v110 import classify_arm, annotate_patch_len_deltas
target = classify_arm(Path('acceptance/swebench/post_specdd_v1103_n75/rep_1/predictions.json'))
baseline_rows = {r['instance_id']: r for r in json.loads(Path('acceptance/v1102_taxonomy/v1102_n75_full_stack_swebench.json').read_text())['rows']}
annotate_patch_len_deltas(target, baseline_rows)
rows = sorted(({'instance_id': iid, **d} for iid, d in target.items()),
              key=lambda r: r['instance_id'])
Path('${V1103_TAX}').write_text(json.dumps({'rows': rows}, indent=2))
print(f'wrote v1103 r1 taxonomy: {len(rows)} rows')
"
fi

if [ ! -f "${PREDS}" ]; then
    echo "FATAL: ${PREDS} not found — n=75 bench has not completed." >&2
    exit 2
fi

echo "=== [1/4] Save run_id_manifest (${REP}) ==="
python -m scripts.save_run_id_manifest "${PREDS}"

echo "=== [2/4] Generate v1.10.4 taxonomy via compare_v110.py ==="
mkdir -p "$(dirname "${V1104_TAX}")"
python -m scripts.compare_v110 \
    --predictions "${PREDS}" \
    --baseline-taxonomy "${V1103_TAX}" \
    --label "v1.10.4 ${REP}" \
    || true

python -c "
import json
from pathlib import Path
from scripts.compare_v110 import classify_arm, annotate_patch_len_deltas
target = classify_arm(Path('${PREDS}'))
baseline_rows = {r['instance_id']: r for r in json.loads(Path('${V1103_TAX}').read_text())['rows']}
annotate_patch_len_deltas(target, baseline_rows)
rows = sorted(({'instance_id': iid, **d} for iid, d in target.items()),
              key=lambda r: r['instance_id'])
Path('${V1104_TAX}').write_text(json.dumps({'rows': rows}, indent=2))
print(f'wrote v1104 taxonomy (${REP}): {len(rows)} rows')
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
    run_id='luxe_v1104_n75_${REP}',
)
write_harness_summary(results, out / 'harness_summary.json')
print('Docker harness complete:', len(results), 'instances')
"

echo "=== [4/4] Cross-cycle analysis (v1.10.4 ${REP} vs v1.10.3 rep_1 baseline) ==="
python -c "
import json, sys
from pathlib import Path
sys.path.insert(0, '.')
import scripts.analyze_v110_harness as A

A.V110_HARNESS = Path('${HARNESS_DIR}/harness_summary.json')
A.V110_TAX = Path('${V1104_TAX}')
A.V19_HARNESS = Path('acceptance/swebench/post_specdd_v1103_n75/rep_1/harness/harness_summary.json')
A.V19_TAX = Path('${V1103_TAX}')
A.RECOVERIES = []
A.main()
" 2>&1 || true

echo ""
echo "=== Pipeline complete (${REP}). Review:"
echo "  v1.10.4 taxonomy:  ${V1104_TAX}"
echo "  Docker summary:    ${HARNESS_DIR}/harness_summary.json"
echo "  Run id manifest:   ${CYCLE_DIR}/run_id_manifest.json"
