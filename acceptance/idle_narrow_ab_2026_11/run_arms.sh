#!/bin/bash
# narrow idle test A/B driver (2026-11-03, m5; copied from bash_noop_ab_2026_11).
# Usage (from the bench worktree):
#   run_arms.sh <model_id> <label> <reps> <arm>=<ENV...> [<arm>=<ENV...> ...]
# Interleaved: for each rep, every arm runs the 10 fixtures once, so arms
# share the same hours of substrate drift. Output: $D/<label>-<arm>/, one
# variant per rep (<label>-rep<N>) so cells never collide on resume.
# A bare "ctl" (no '=') is the shipped-defaults arm.
# IDS="id1 id2" restricts to those fixtures (the step-2 slice); default --all.
export PATH=/opt/homebrew/bin:$HOME/.local/bin:$PATH
D=acceptance/idle_narrow_ab_2026_11
MODEL=$1; LABEL=$2; REPS=$3; shift 3
SEL=(--all)
if [[ -n $IDS ]]; then SEL=(); for i in $IDS; do SEL+=(--id "$i"); done; fi
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
