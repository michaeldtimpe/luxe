#!/bin/zsh
# SWE-bench Verified n=75 overnight (2026-11-06), current HEAD, m5.
# Order: ctxonly (128K ctx only), unbudgeted (arm A overlay), control (stock budgeted).
# Each arm: preds-only run, then official swebench 4.1.0 Docker grading (colima).
# Control runs last so the two treatment arms finish first; it can be killed.
cd ~/Downloads/luxe
OUT=$HOME/Downloads/luxe/acceptance/swebench/n75_ab_2026_11_06
mkdir -p $OUT
echo $$ > $OUT/runner.pid
echo "start $(date "+%F %T") head=$(git rev-parse --short HEAD)" >> $OUT/STATUS
export PYTHONFAULTHANDLER=1 PYTHONUNBUFFERED=1 LUXE_LOG_TOOL_CALLS=1
SUB=benchmarks/swebench/subsets/v1_baseline_n75.json
for arm in ctxonly unbudgeted control; do
  case $arm in
    ctxonly)    cfg=configs/single_64gb_swebench_ctxonly.yaml;    to=1800;;
    unbudgeted) cfg=configs/single_64gb_swebench_unbudgeted.yaml; to=5400;;
    control)    cfg=configs/single_64gb_swebench.yaml;            to=1800;;
  esac
  echo "$arm run start $(date "+%F %T") cfg=$cfg" >> $OUT/STATUS
  .venv/bin/python -m benchmarks.swebench.run --config $cfg --subset $SUB \
    --per-instance-timeout $to --output $OUT/$arm/ > $OUT/$arm.log 2>&1
  echo "$arm run exit=$? $(date "+%F %T")" >> $OUT/STATUS
  colima status >/dev/null 2>&1 || colima start >> $OUT/grade.log 2>&1
  export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
  mkdir -p $OUT/$arm/harness
  ( cd $OUT/$arm/harness && PYTHONPATH=$HOME/Downloads/luxe ~/.venvs/swebench-grading/bin/python -u -c "
from pathlib import Path
from benchmarks.swebench.harness import run_harness, write_harness_summary
out = Path('.').resolve()
r = run_harness(Path('../predictions.json').resolve(), output_dir=out, run_id='n75_$arm')
write_harness_summary(r, out / 'harness_summary.json')
print('$arm resolved', sum(x.resolved for x in r.values()), '/', len(r), 'errors', sum(bool(x.error) for x in r.values()))
" ) >> $OUT/grade.log 2>&1
  echo "$arm grade exit=$? $(date "+%F %T")" >> $OUT/STATUS
done
echo "ALL DONE $(date "+%F %T")" >> $OUT/STATUS
