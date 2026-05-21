#!/usr/bin/env bash
# v1.11 Phase D — n=75 x 3 reps ship-gate bench with the Phase B lever LIVE.
#
# Apples-to-apples cohort (v1_baseline_n75.json) matching every prior cycle.
# Adaptive policy ON. Predictions only here; Docker harness + cohort-shift
# run per-rep via post_v111_n75_pipeline.sh after each rep completes.
#
# Wall budget: ~6h/rep x 3 = ~18h (serial, background). Each rep's
# post-pipeline (~35m Docker) runs inline so partial results are available
# as reps land.
#
# Ship floors (vs v1.10.5 baseline, evaluated by the post-pipeline):
#   empty_patch <= 13 best-of-3, strong+plausible >= 39, 0 deterministic
#   losses (cohort_shift_3x3), Docker resolves >= 37.
#
# Pipeline progress: acceptance/v111_phaseD_pipeline.log

set -uo pipefail

PIPE_LOG="acceptance/v111_phaseD_pipeline.log"
mkdir -p "$(dirname "$PIPE_LOG")"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$PIPE_LOG"
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

export LUXE_ADAPTIVE_POLICY=1
export LUXE_LOG_TOOL_CALLS=1

log "Phase D pipeline start. LUXE_ADAPTIVE_POLICY=$LUXE_ADAPTIVE_POLICY (Phase B lever LIVE)"

for rep in rep_1 rep_2 rep_3; do
    OUT="acceptance/swebench/post_v111_n75/${rep}"
    if [[ -f "${OUT}/predictions.json" ]]; then
        log "SKIP bench ${rep} — predictions.json already present"
    else
        mkdir -p "$OUT"
        log "START n=75 ${rep}"
        python -m benchmarks.swebench.run \
            --subset benchmarks/swebench/subsets/v1_baseline_n75.json \
            --output "$OUT" \
            > "${OUT}/stdout.log" 2>&1
        rc=$?
        log "DONE n=75 ${rep} rc=${rc}"
    fi
    if [[ -f "${OUT}/predictions.json" ]]; then
        log "POST-PIPELINE ${rep} (manifest + Docker + cohort-shift)"
        bash scripts/post_v111_n75_pipeline.sh "${rep}" >> "$PIPE_LOG" 2>&1 \
            && log "POST-PIPELINE ${rep} OK" \
            || log "POST-PIPELINE ${rep} FAILED (continuing)"
    else
        log "WARN ${rep} predictions.json missing — skipping post-pipeline"
    fi
done

# --- strict gate: cohort_shift_3x3 v1.11 vs v1.10.5 (3-rep x 3-rep matrix) ---
A1="acceptance/v1105_taxonomy/v1105_n75_full_stack_swebench.json"
A2="acceptance/v1105_taxonomy/v1105_n75_rep_2_full_stack_swebench.json"
A3="acceptance/v1105_taxonomy/v1105_n75_rep_3_full_stack_swebench.json"
B1="acceptance/v111_taxonomy/v111_n75_full_stack_swebench.json"
B2="acceptance/v111_taxonomy/v111_n75_rep_2_full_stack_swebench.json"
B3="acceptance/v111_taxonomy/v111_n75_rep_3_full_stack_swebench.json"
if [[ -f "$B1" && -f "$B2" && -f "$B3" ]]; then
    log "COHORT-SHIFT 3x3 v1.11 vs v1.10.5 (the strictest ship gate)"
    python -m scripts.cohort_shift_3x3 \
        --cycle-a v1.10.5 "$A1" "$A2" "$A3" \
        --cycle-b v1.11 "$B1" "$B2" "$B3" \
        --snapshot-out acceptance/v111_taxonomy/v111_vs_v1105_snapshot.jsonl \
        >> "$PIPE_LOG" 2>&1
    log "cohort_shift_3x3 exit=$?  (0=clean, nonzero=deterministic regression — HOLD)"
else
    log "WARN cohort_shift_3x3 skipped — not all 3 v111 taxonomies present"
fi

log "Phase D pipeline complete. Review acceptance/swebench/post_v111_n75/."
