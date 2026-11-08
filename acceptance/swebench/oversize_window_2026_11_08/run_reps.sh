#!/bin/zsh
# LUXE_READ_OVERSIZE_WINDOW=1 arm, n=75 x 3, m5, 2026-11-08. Code: worktree
# ~/luxe-bench-tmp/oversize-window (branch read-oversize-window = 60bdc94 + the
# opt-in lever). Control = the 3 control reps of n75_ab_2026_11_06 (65fd43c;
# no src change since, flag OFF is byte-identical, same oMLX 0.7.3 + weights).
cd ~/luxe-bench-tmp/oversize-window
OUT=$HOME/Downloads/luxe/acceptance/swebench/oversize_window_2026_11_08
echo $$ > $OUT/reps.pid
echo "start $(date "+%F %T") head=$(git rev-parse --short HEAD)" >> $OUT/STATUS
export PYTHONFAULTHANDLER=1 PYTHONUNBUFFERED=1 LUXE_LOG_TOOL_CALLS=1 LUXE_READ_OVERSIZE_WINDOW=1
SUB=$HOME/Downloads/luxe/benchmarks/swebench/subsets/v1_baseline_n75.json
DS=$HOME/Downloads/luxe/benchmarks/swebench/subsets/raw/verified.jsonl
for rep in rep1 rep2 rep3; do
  D=$OUT/$rep/window; mkdir -p $D/harness
  echo "$rep window run start $(date "+%F %T")" >> $OUT/STATUS
  .venv/bin/python -m benchmarks.swebench.run --config configs/single_64gb_swebench.yaml --subset $SUB \
    --dataset $DS --per-instance-timeout 1800 --output $D/ > $OUT/$rep/window.log 2>&1
  echo "$rep window run exit=$? $(date "+%F %T")" >> $OUT/STATUS
  colima status >/dev/null 2>&1 || colima start >> $OUT/grade.log 2>&1
  export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
  ( cd $D/harness && PYTHONPATH=$HOME/luxe-bench-tmp/oversize-window ~/.venvs/swebench-grading/bin/python -u -c "
from pathlib import Path
from benchmarks.swebench.harness import run_harness, write_harness_summary
out = Path('.').resolve()
r = run_harness(Path('../predictions.json').resolve(), output_dir=out, run_id='ow_${rep}')
write_harness_summary(r, out / 'harness_summary.json')
print('$rep window resolved', sum(x.resolved for x in r.values()), '/', len(r), 'errors', sum(bool(x.error) for x in r.values()))
" ) >> $OUT/grade.log 2>&1
  echo "$rep window grade exit=$? $(date "+%F %T")" >> $OUT/STATUS
  docker ps -a --format '{{.Names}}' | grep '^sweb.eval' | xargs -r docker rm -f >> $OUT/grade.log 2>&1
done
colima stop >> $OUT/grade.log 2>&1
echo "REPS DONE $(date "+%F %T")" >> $OUT/STATUS
