#!/bin/zsh
# LUXE_FAILED_OUTPUT SWE-bench slice (2026-11-19, m5; modeled on
# acceptance/swebench/diag_misses_2026_11/run.sh). The real test of the flag:
# 13 C/D flip targets + 12 no-harm instances (subset fo_slice_2026_11.json,
# per_instance_role says which), arms control vs fo, 3 reps interleaved
# (each rep runs every arm, so arms share substrate drift). Grading runs
# after all predictions. LUXE_SAVE_MESSAGES is exported by the swebench
# runner by default (adapter.py).
# Usage: swebench_slice.sh [reps]   (default 3). Output: $OUT/<arm>-rep<N>/.
cd ~/Downloads/luxe-bench || exit 1
OUT=$HOME/Downloads/luxe/acceptance/failed_output_ab_2026_11/swebench
mkdir -p $OUT
echo $$ > $OUT/runner.pid
export PYTHONFAULTHANDLER=1 PYTHONUNBUFFERED=1 LUXE_LOG_TOOL_CALLS=1
SUB=$HOME/Downloads/luxe/benchmarks/swebench/subsets/fo_slice_2026_11.json
cfg=configs/single_64gb_swebench.yaml
REPS=${1:-3}
typeset -A ARM_ENV
ARM_ENV=(control "" fo "LUXE_FAILED_OUTPUT=1")
ARMS=(control fo)

grade() {  # $1 = <arm>-rep<N>
  local cell=$1
  colima status >/dev/null 2>&1 || colima start >> $OUT/grade.log 2>&1
  export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
  mkdir -p $OUT/$cell/harness
  ( cd $OUT/$cell/harness && PYTHONPATH=$HOME/Downloads/luxe ~/.venvs/swebench-grading/bin/python -u -c "
from pathlib import Path
from benchmarks.swebench.harness import run_harness, write_harness_summary
out = Path('.').resolve()
r = run_harness(Path('../predictions.json').resolve(), output_dir=out, run_id='fo_$cell')
write_harness_summary(r, out / 'harness_summary.json')
print('$cell resolved', sum(x.resolved for x in r.values()), '/', len(r), 'errors', sum(bool(x.error) for x in r.values()))
" ) >> $OUT/grade.log 2>&1
}

for rep in $(seq 1 $REPS); do
  for arm in $ARMS; do
    cell=$arm-rep$rep
    echo "REP $rep ARM $arm start $(date "+%F %T")" >> $OUT/arms.log
    env ${=ARM_ENV[$arm]} .venv/bin/python -m benchmarks.swebench.run \
      --config $cfg --subset $SUB --per-instance-timeout 1800 \
      --output $OUT/$cell/ > $OUT/$cell.log 2>&1
    rc=$?
    echo "REP $rep ARM $arm end $(date "+%F %T") rc=$rc" >> $OUT/arms.log
  done
done
# Grade after all predictions so the GPU is never idle waiting on Docker
# (grading is CPU-bound; the next GPU job can start while this runs).
echo "PREDICT_DONE fo_slice $(date "+%F %T")" >> $OUT/arms.log
for rep in $(seq 1 $REPS); do
  for arm in $ARMS; do grade $arm-rep$rep; done
done
echo "DONE fo_slice $(date "+%F %T")" >> $OUT/arms.log
