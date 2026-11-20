#!/bin/bash
# Waits for the idle-wide re-run to finish, moves the bench worktree to current
# origin/main (adds R4 doc-config coverage recording; grading-only), then runs
# ctl (shipped defaults on oMLX 0.8.0) vs ow (LUXE_READ_OVERSIZE_WINDOW=1).
P=$HOME/Downloads/luxe/acceptance/idle_wide_rerun_2026_11/arms.log
D=$HOME/Downloads/luxe/acceptance/baseline_080_2026_11
echo "QUEUED $(date '+%F %T') waiting for DONE in $P" >> $D/arms.log
until grep -q '^DONE' "$P" 2>/dev/null; do sleep 60; done
cd ~/Downloads/luxe-bench || exit 1
git fetch -q origin && git checkout -q --detach origin/main
echo "BENCH at $(git log --oneline -1) oMLX $(/bin/ls /opt/homebrew/Cellar/omlx/ | tr '\n' ' ')" >> $D/arms.log
exec $D/run_arms.sh Qwen3.6-35B-A3B-6bit champ 3 ctl "ow=LUXE_READ_OVERSIZE_WINDOW=1"
