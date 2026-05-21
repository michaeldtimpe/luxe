#!/usr/bin/env bash
# v1.11 Phase 3a — archetype-6 preflight (3 reps) + BFCL irrelevance probe.
#
# Runs the full Stage 3 archetype-validation pipeline serially in the
# background. Wall budget: ~5-6h for archetypes + ~70 min BFCL.
#
# Outputs (all under acceptance/):
#   swebench/v1110_archetype_n6/rep_{1,2,3}/  — per-rep predictions
#   bfcl/v1110_irrelevance_probe/rep_1/        — irrelevance subset
#
# Logs:
#   logs land at <output>/stdout.log
#   pipeline progress at acceptance/v1110_phase3a_pipeline.log
#
# Adaptive policy ON; per-instance LUXE_LOG_TOOL_CALLS for adaptive_state
# events in the run's events.jsonl.

set -uo pipefail

PIPE_LOG="acceptance/v1110_phase3a_pipeline.log"
mkdir -p "$(dirname "$PIPE_LOG")"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$PIPE_LOG"
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

export LUXE_ADAPTIVE_POLICY=1
export LUXE_LOG_TOOL_CALLS=1

log "Pipeline start. LUXE_ADAPTIVE_POLICY=$LUXE_ADAPTIVE_POLICY"

# --- archetype-6 reps ---
for rep in rep_1 rep_2 rep_3; do
    OUT="acceptance/swebench/v1110_archetype_n6/${rep}"
    if [[ -f "${OUT}/predictions.json" ]]; then
        log "SKIP ${rep} — predictions.json already present"
        continue
    fi
    mkdir -p "$OUT"
    log "START archetype-6 ${rep}"
    python -m benchmarks.swebench.run \
        --subset benchmarks/swebench/subsets/v1110_archetype_n6.json \
        --output "$OUT" \
        > "${OUT}/stdout.log" 2>&1
    rc=$?
    log "DONE archetype-6 ${rep} rc=${rc}"
    if [[ ! -f "${OUT}/predictions.json" ]]; then
        log "WARN ${rep} predictions.json missing — continuing anyway"
    fi
done

# --- BFCL irrelevance probe ---
BFCL_OUT="acceptance/bfcl/v1110_irrelevance_probe/rep_1"
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
    --label-new v1.11_phase3a \
    >> "$PIPE_LOG" 2>&1
log "bfcl gate exit=$?"

log "Pipeline complete."
