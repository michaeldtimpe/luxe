#!/bin/bash
# Live check for the `_`-stem fix (2026-11-20, m5): isomer-implement-healthcheck x3 on
# shipped defaults at the fix commit. Waits for the image-exec follow-up's predictions
# (PREDICT_DONE) so the GPU is not shared; grading of that run may overlap (CPU only).
# Usage: queue.sh <fix-sha>
export PATH=/opt/homebrew/bin:$HOME/.local/bin:$PATH
SHA=$1
D=$HOME/Downloads/luxe/acceptance/cleanup_underscore_2026_11
P=$HOME/Downloads/luxe/acceptance/swebench/image_exec_2026_11/followup/arms.log
echo "QUEUED $(date '+%F %T') sha=$SHA waiting for PREDICT_DONE in $P" >> $D/arms.log
until grep -q '^PREDICT_DONE' "$P" 2>/dev/null; do sleep 60; done
cd ~/Downloads/luxe-land || exit 1
git fetch -q origin && git checkout -q --detach "$SHA" || exit 1
echo "BENCH at $(git log --oneline -1)" >> $D/arms.log
uv sync -q --extra chat --extra dev --extra analyzers --extra web >> $D/run.log 2>&1
for rep in 1 2 3; do
  V=$D/.variants_rep$rep.yaml
  printf 'variants:\n  - {model_label: fix-rep%s, model_id: Qwen3.6-35B-A3B-6bit}\n' "$rep" > "$V"
  echo "REP $rep start $(date '+%F %T')" >> $D/arms.log
  uv run python -m benchmarks.maintain_suite.run --id isomer-implement-healthcheck \
    --variants "$V" --output "$D/fix/" --per-fixture-timeout 1800 \
    --work-dir ~/.luxe/bench-workspace >> $D/run.log 2>&1
  rc=$?
  echo "REP $rep end $(date '+%F %T') rc=$rc" >> $D/arms.log
done
echo "DONE $(date '+%F %T')" >> $D/arms.log
