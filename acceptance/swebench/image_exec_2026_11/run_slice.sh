#!/bin/zsh
# SWE-bench image-exec slice (2026-11-20, m5; from
# acceptance/failed_output_ab_2026_11/swebench_slice.sh). Arms:
#   control  shipped defaults
#   fo       LUXE_FAILED_OUTPUT=1
#   ix       --image-exec + configs/single_64gb_swebench_exec.yaml
#            + LUXE_FAILED_OUTPUT=1 + LUXE_BASH_NOOP=strip
# Interleaved (each rep runs every arm). Grading after all predictions so the
# GPU never waits on Docker. See PLAN.md for gates.
# Usage: run_slice.sh [reps]              (default 3, fo_slice_2026_11.json)
#   SUB=<subset.json> ARMS="ix" TAG=smoke run_slice.sh 1   (the G2 smoke)
# Output: $OUT/<arm>-rep<N>/ (predictions + harness/harness_summary.json),
# arms.log start/end/PREDICT_DONE/DONE, grade.log.
cd ~/Downloads/luxe-bench || exit 1
TAG=${TAG:-slice}
OUT=$HOME/Downloads/luxe/acceptance/swebench/image_exec_2026_11/$TAG
mkdir -p $OUT
echo $$ > $OUT/runner.pid
export PYTHONFAULTHANDLER=1 PYTHONUNBUFFERED=1 LUXE_LOG_TOOL_CALLS=1
export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
SUB=${SUB:-$HOME/Downloads/luxe/benchmarks/swebench/subsets/fo_slice_2026_11.json}
REPS=${1:-3}
# Harness workers for grading (2026-11-20: 2 took ~3.7 h for 9 cells on m5's 8-CPU colima).
GRADE_WORKERS=${GRADE_WORKERS:-4}
typeset -A ARM_ENV ARM_CFG ARM_FLAGS
# Follow-up arms (2026-11-20, after G3's 23/75 step-limit hits; TAG=followup ARMS="ix ix60 ix60nb"):
#   ix60     ix with max_steps 60 (configs/single_64gb_swebench_exec_60.yaml)
#   ix60nb   ix60 + --no-early-bail --no-write-pressure --no-action-density-gate
IXENV="LUXE_FAILED_OUTPUT=1 LUXE_BASH_NOOP=strip"
ARM_ENV=(control "" fo "LUXE_FAILED_OUTPUT=1" ix "$IXENV" ix60 "$IXENV" ix60nb "$IXENV")
ARM_CFG=(control configs/single_64gb_swebench.yaml fo configs/single_64gb_swebench.yaml
         ix configs/single_64gb_swebench_exec.yaml
         ix60 configs/single_64gb_swebench_exec_60.yaml
         ix60nb configs/single_64gb_swebench_exec_60.yaml)
ARM_FLAGS=(control "" fo "" ix "--image-exec" ix60 "--image-exec"
           ix60nb "--image-exec --no-early-bail --no-write-pressure --no-action-density-gate")
ARMS=(${=ARMS:-control fo ix})

colima status >/dev/null 2>&1 || colima start >> $OUT/grade.log 2>&1

grade() {  # $1 = <arm>-rep<N>
  local cell=$1
  colima status >/dev/null 2>&1 || colima start >> $OUT/grade.log 2>&1
  mkdir -p $OUT/$cell/harness
  ( cd $OUT/$cell/harness && PYTHONPATH=$HOME/Downloads/luxe ~/.venvs/swebench-grading/bin/python -u -c "
from pathlib import Path
from benchmarks.swebench.harness import run_harness, write_harness_summary
out = Path('.').resolve()
r = run_harness(Path('../predictions.json').resolve(), output_dir=out, run_id='ix_${TAG}_$cell', max_workers=$GRADE_WORKERS)
write_harness_summary(r, out / 'harness_summary.json')
print('$cell resolved', sum(x.resolved for x in r.values()), '/', len(r), 'errors', sum(bool(x.error) for x in r.values()))
" ) >> $OUT/grade.log 2>&1
}

for rep in $(seq 1 $REPS); do
  for arm in $ARMS; do
    cell=$arm-rep$rep
    echo "REP $rep ARM $arm start $(date "+%F %T")" >> $OUT/arms.log
    env ${=ARM_ENV[$arm]} .venv/bin/python -m benchmarks.swebench.run \
      --config ${ARM_CFG[$arm]} ${=ARM_FLAGS[$arm]} --subset $SUB \
      --per-instance-timeout 1800 --output $OUT/$cell/ > $OUT/$cell.log 2>&1
    rc=$?
    echo "REP $rep ARM $arm end $(date "+%F %T") rc=$rc" >> $OUT/arms.log
  done
done
echo "PREDICT_DONE $TAG $(date "+%F %T")" >> $OUT/arms.log
for rep in $(seq 1 $REPS); do
  for arm in $ARMS; do grade $arm-rep$rep; done
done
echo "DONE $TAG $(date "+%F %T")" >> $OUT/arms.log
