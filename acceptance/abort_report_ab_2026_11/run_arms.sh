#!/bin/bash
# abort-report A/B driver (2026-11-19, m5; copied from idle_capped_ab_2026_11).
# Usage (from the bench worktree):
#   run_arms.sh <model_id> <label> <reps> <arm>=<ENV...> [<arm>=<ENV...> ...]
# Interleaved: for each rep, every arm runs the 10 fixtures once, so arms
# share the same hours of substrate drift. Output: $D/<label>-<arm>/, one
# variant per rep (<label>-rep<N>) so cells never collide on resume.
# A bare "ctl" (no '=') is the shipped-defaults arm.
# IDS="id1 id2" restricts to those fixtures (the step-2 slice); default --all.
# WARM-UP (new here): before rep 1, one throwaway run of $WARMUP_ID (default
# isomer-implement-healthcheck) on shipped defaults into $D/warmup/, NOT
# counted (analyze.py reads only <label>-<arm>/). The previous A/B's first
# arm ran in a slow-decode window. WARMUP_ID=none skips it.
export PATH=/opt/homebrew/bin:$HOME/.local/bin:$PATH
D=$HOME/Downloads/luxe/acceptance/abort_report_ab_2026_11
MODEL=$1; LABEL=$2; REPS=$3; shift 3
SEL=(--all)
if [[ -n $IDS ]]; then SEL=(); for i in $IDS; do SEL+=(--id "$i"); done; fi
WARMUP_ID=${WARMUP_ID:-isomer-implement-healthcheck}
if [[ $WARMUP_ID != none ]]; then
  V=$D/.variants_${LABEL}_warmup.yaml
  printf 'variants:\n  - {model_label: %s-warmup, model_id: %s}\n' "$LABEL" "$MODEL" > "$V"
  echo "WARMUP $WARMUP_ID start $(date "+%F %T")" >> $D/arms.log
  uv run python -m benchmarks.maintain_suite.run --id "$WARMUP_ID" \
    --variants "$V" --output "$D/warmup/" \
    --per-fixture-timeout 1800 --work-dir ~/.luxe/bench-workspace \
    >> "$D/warmup.log" 2>&1
  rc=$?
  echo "WARMUP end $(date "+%F %T") rc=$rc" >> $D/arms.log
fi
for rep in $(seq 1 "$REPS"); do
  V=$D/.variants_${LABEL}_rep${rep}.yaml
  printf 'variants:\n  - {model_label: %s-rep%s, model_id: %s}\n' "$LABEL" "$rep" "$MODEL" > "$V"
  for spec in "$@"; do
    arm=${spec%%=*}; envs=""; [[ $spec == *=* ]] && envs=${spec#*=}
    echo "REP $rep ARM $arm start $(date "+%F %T")" >> $D/arms.log
    env $envs uv run python -m benchmarks.maintain_suite.run "${SEL[@]}" \
      --variants "$V" --output "$D/$LABEL-$arm/" \
      --per-fixture-timeout 1800 --work-dir ~/.luxe/bench-workspace \
      >> "$D/$LABEL-$arm.log" 2>&1
    rc=$?
    echo "REP $rep ARM $arm end $(date "+%F %T") rc=$rc" >> $D/arms.log
  done
done
echo "DONE $LABEL $(date "+%F %T")" >> $D/arms.log
