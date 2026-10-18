#!/usr/bin/env bash
# Rep-major queue for the harness bake-off: every (model x harness) cell gets
# rep N before any cell gets rep N+1, so a full first picture lands early and
# the queue can be stopped once a cell is settled. Resumable: harness_ab.py
# skips fixtures already DONE in each run dir's state.json.
#
#   OUT=acceptance/harness_ab_2026_10 nohup scripts/harness_ab_queue.sh > $OUT/queue.log 2>&1 &
#
# Env: OUT (required), REPS (default "1 2 3"), HARNESSES (default
# "opencode qwen codex claude codex-cat"), MODELS (default: champion,
# Qwen3.8-27B-8bit, Muse-Glimmer-30B-6bit), LUXE_CONTROL (default 1: run
# luxe's own offline deps-audit control first; resumable, so a restart that
# finds it done moves straight on).
set -u
cd "$(dirname "$0")/.."
: "${OUT:?set OUT to the acceptance dir}"
REPS=${REPS:-"1 2 3"}
HARNESSES=${HARNESSES:-"opencode qwen codex claude codex-cat"}
MODELS=${MODELS:-"Qwen3.6-35B-A3B-6bit:champ:1200 Qwen3.8-27B-8bit:q38:3600 Muse-Glimmer-30B-6bit:glim:3600"}

if [ "${LUXE_CONTROL:-1}" = 1 ]; then
  echo "=== $(date '+%F %T') luxe-offline control (deps-audit x3)"
  OUT="$OUT" scripts/harness_ab_luxe_offline.sh
  echo "=== $(date '+%F %T') rc=$? luxe-offline control"
fi

for rep in $REPS; do
  for m in $MODELS; do
    IFS=: read -r model tag timeout <<<"$m"
    for h in $HARNESSES; do
      dir="$OUT/$h/$tag/rep$rep"
      echo "=== $(date '+%F %T') rep$rep $tag $h -> $dir"
      uv run python scripts/harness_ab.py --harness "$h" --model "$model" \
        --output "$dir" --all --timeout "$timeout" --label "$h/$tag/rep$rep"
      echo "=== $(date '+%F %T') rc=$? rep$rep $tag $h"
    done
  done
done
echo "=== $(date '+%F %T') queue finished"
