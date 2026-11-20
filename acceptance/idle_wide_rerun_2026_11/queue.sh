#!/bin/bash
# Waits for the image-exec slice to finish predicting (GPU free), then runs the
# idle-wide re-run: ctl = LUXE_FAILED_OUTPUT=1, wide = + LUXE_IDLE_NARROW=wide.
S=$HOME/Downloads/luxe/acceptance/swebench/image_exec_2026_11/slice/arms.log
D=$HOME/Downloads/luxe/acceptance/idle_wide_rerun_2026_11
echo "QUEUED $(date '+%F %T') waiting for PREDICT_DONE in $S" >> $D/arms.log
until grep -q '^PREDICT_DONE' "$S" 2>/dev/null; do sleep 60; done
cd ~/Downloads/luxe-bench || exit 1
exec $D/run_arms.sh Qwen3.6-35B-A3B-6bit champ 3 "ctl=LUXE_FAILED_OUTPUT=1" "wide=LUXE_FAILED_OUTPUT=1 LUXE_IDLE_NARROW=wide"
