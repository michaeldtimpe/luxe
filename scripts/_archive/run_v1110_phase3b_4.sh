#!/usr/bin/env bash
# v1.11 Phase 3b ablation sweep + Phase 4 n=14 smoke.
#
# Phase 3b: 2 ablation conditions × 6 archetypes × 1 rep = 12 runs (~36 min)
#   A = NO_WRITE disabled, score_trend enabled
#   B = NO_WRITE enabled,  score_trend disabled
#
# Phase 4: n=14 smoke × 3 reps with both signals ON (~2h)
#   Comparison baseline: v1.10.5 n=75 filtered to the 14 instances
#
# Wall budget: ~2.5h
# Log: acceptance/v1110_phase3b4_pipeline.log

set -uo pipefail

PIPE_LOG="acceptance/v1110_phase3b4_pipeline.log"
mkdir -p "$(dirname "$PIPE_LOG")"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$PIPE_LOG"
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

export LUXE_ADAPTIVE_POLICY=1
export LUXE_LOG_TOOL_CALLS=1

log "Phase 3b+4 pipeline start. LUXE_ADAPTIVE_POLICY=$LUXE_ADAPTIVE_POLICY"

# --- Phase 3b ablation A: no_write disabled ---
A_OUT="acceptance/swebench/v1110_phase3b_no_write_off/rep_1"
if [[ -f "${A_OUT}/predictions.json" ]]; then
    log "SKIP Phase 3b A — predictions.json already present"
else
    mkdir -p "$A_OUT"
    log "START Phase 3b A (LUXE_ADAPTIVE_NO_WRITE=0)"
    LUXE_ADAPTIVE_NO_WRITE=0 python -m benchmarks.swebench.run \
        --subset benchmarks/swebench/subsets/v1110_archetype_n6.json \
        --output "$A_OUT" \
        > "${A_OUT}/stdout.log" 2>&1
    log "DONE Phase 3b A rc=$?"
fi

# --- Phase 3b ablation B: score_trend disabled ---
B_OUT="acceptance/swebench/v1110_phase3b_score_trend_off/rep_1"
if [[ -f "${B_OUT}/predictions.json" ]]; then
    log "SKIP Phase 3b B — predictions.json already present"
else
    mkdir -p "$B_OUT"
    log "START Phase 3b B (LUXE_ADAPTIVE_SCORE_TREND=0)"
    LUXE_ADAPTIVE_SCORE_TREND=0 python -m benchmarks.swebench.run \
        --subset benchmarks/swebench/subsets/v1110_archetype_n6.json \
        --output "$B_OUT" \
        > "${B_OUT}/stdout.log" 2>&1
    log "DONE Phase 3b B rc=$?"
fi

# --- Phase 4 n=14 smoke (3 reps, both signals on) ---
for rep in rep_1 rep_2 rep_3; do
    OUT="acceptance/swebench/v1110_smoke_n14/${rep}"
    if [[ -f "${OUT}/predictions.json" ]]; then
        log "SKIP Phase 4 ${rep} — predictions.json already present"
        continue
    fi
    mkdir -p "$OUT"
    log "START Phase 4 n=14 smoke ${rep}"
    python -m benchmarks.swebench.run \
        --subset benchmarks/swebench/subsets/v19_smoke_n14.json \
        --output "$OUT" \
        > "${OUT}/stdout.log" 2>&1
    log "DONE Phase 4 n=14 ${rep} rc=$?"
done

log "Phase 3b+4 pipeline complete."
