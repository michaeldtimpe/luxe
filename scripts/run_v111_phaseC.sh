#!/usr/bin/env bash
# v1.11 Phase C — activation probe for the score_trend->soft_anchor lever.
#
# Union subset (archetype-6 + 2 deterministic empties) x 3 reps + BFCL
# irrelevance anchor. Adaptive policy ON so the Phase B lever is live.
#
# Gates (evaluated post-run by analyze_v111_phaseC.py):
#   (a) soft_anchor_collapse_promote_fired on >=1 empty instance
#   (b) 0 deterministic archetype regressions vs v1.10.5 baseline
#   (c) BFCL irrelevance 240/240
#
# Wall budget: ~2-3h (24 swebench runs ~2h + BFCL ~70m).
# Pipeline progress: acceptance/v111_phaseC_pipeline.log

set -uo pipefail

PIPE_LOG="acceptance/v111_phaseC_pipeline.log"
mkdir -p "$(dirname "$PIPE_LOG")"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$PIPE_LOG"
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

export LUXE_ADAPTIVE_POLICY=1
export LUXE_LOG_TOOL_CALLS=1

log "Phase C pipeline start. LUXE_ADAPTIVE_POLICY=$LUXE_ADAPTIVE_POLICY (Phase B lever LIVE)"

# --- union subset x3 reps ---
for rep in rep_1 rep_2 rep_3; do
    OUT="acceptance/swebench/v111_phaseC_n8/${rep}"
    if [[ -f "${OUT}/predictions.json" ]]; then
        log "SKIP ${rep} — predictions.json already present"
        continue
    fi
    mkdir -p "$OUT"
    log "START phaseC-n8 ${rep}"
    python -m benchmarks.swebench.run \
        --subset benchmarks/swebench/subsets/v111_phaseC_n8.json \
        --output "$OUT" \
        > "${OUT}/stdout.log" 2>&1
    rc=$?
    log "DONE phaseC-n8 ${rep} rc=${rc}"
    if [[ ! -f "${OUT}/predictions.json" ]]; then
        log "WARN ${rep} predictions.json missing — continuing anyway"
    fi
done

# --- BFCL irrelevance anchor ---
BFCL_OUT="acceptance/bfcl/v111_phaseC_irrelevance/rep_1"
if [[ -f "${BFCL_OUT}/summary.json" ]]; then
    log "SKIP BFCL irrelevance — summary.json already present"
else
    mkdir -p "$BFCL_OUT"
    log "START BFCL irrelevance probe (~70 min wall)"
    python -m benchmarks.bfcl.run \
        --mode agent \
        --categories irrelevance \
        --output "$BFCL_OUT" \
        > "${BFCL_OUT}/stdout.log" 2>&1
    rc=$?
    log "DONE BFCL irrelevance rc=${rc}"
fi

# --- gate: bfcl_anchor_check irrelevance against Stage 1 anchor ---
log "Running bfcl_anchor_check --categories irrelevance"
python -m scripts.bfcl_anchor_check \
    --anchor acceptance/bfcl/post_v1105_stage1_verify/rep_1 \
    --new "$BFCL_OUT" \
    --categories irrelevance \
    --label-anchor v1.10.5_stage1 \
    --label-new v1.11_phaseC \
    >> "$PIPE_LOG" 2>&1
log "bfcl gate exit=$?"

log "Phase C pipeline complete."
