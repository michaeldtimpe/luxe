#!/bin/bash
# ThinkingCap 4-bit vs Qwen3.6-27B-4bit (m1's code_main/fallback), maintain_suite
# 10 fixtures x REPS, models interleaved per rep. Run from the bench worktree.
# Usage: run_models.sh <reps> <label>=<model_id> [<label>=<model_id> ...]
export PATH=/opt/homebrew/bin:$HOME/.local/bin:$PATH
D=$HOME/Downloads/luxe/acceptance/thinkingcap_4bit_2026_11
REPS=$1; shift
for rep in $(seq 1 "$REPS"); do
  for spec in "$@"; do
    label=${spec%%=*}; model=${spec#*=}
    V=$D/.variants_${label}_rep${rep}.yaml
    printf 'variants:\n  - {model_label: %s-rep%s, model_id: %s}\n' "$label" "$rep" "$model" > "$V"
    echo "REP $rep ARM $label start $(date "+%F %T")" >> $D/arms.log
    uv run python -m benchmarks.maintain_suite.run --all --variants "$V" \
      --output "$D/$label/" --per-fixture-timeout 1800 \
      --work-dir ~/.luxe/bench-workspace >> "$D/$label.log" 2>&1
    echo "REP $rep ARM $label end $(date "+%F %T") rc=$?" >> $D/arms.log
  done
done
echo "DONE thinkingcap_4bit $(date "+%F %T")" >> $D/arms.log
