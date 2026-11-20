#!/bin/bash
# After the 0.8.0 baseline: bench to origin/main (must contain PR #65's data
# candidates), then strict-flag x3 under the arm that left fake.gguf behind.
P=$HOME/Downloads/luxe/acceptance/baseline_080_2026_11/arms.log
D=$HOME/Downloads/luxe/acceptance/cleanup_data_check_2026_11
echo "QUEUED $(date '+%F %T') waiting for DONE in $P" >> $D/arms.log
until grep -q '^DONE' "$P" 2>/dev/null; do sleep 60; done
cd ~/Downloads/luxe-bench || exit 1
git fetch -q origin && git checkout -q --detach origin/main
if ! grep -q '_is_data' src/luxe/cleanup.py; then
  echo "ABORT $(date '+%F %T') origin/main lacks the cleanup data-candidate fix ($(git log --oneline -1))" >> $D/arms.log; exit 1
fi
echo "BENCH at $(git log --oneline -1)" >> $D/arms.log
export IDS=lpe-rope-calc-implement-strict-flag WARMUP_ID=none
exec $D/run_arms.sh Qwen3.6-35B-A3B-6bit champ 3 "wide=LUXE_FAILED_OUTPUT=1 LUXE_IDLE_NARROW=wide"
