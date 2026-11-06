#!/bin/zsh
# Reps 2 and 3 of ctxonly vs control (2026-11-06, m5). Serial like rep 1 (run_n75.sh);
# arm order alternates (rep2: control, ctxonly; rep3: ctxonly, control) to spread drift.
cd ~/Downloads/luxe
OUT=$HOME/Downloads/luxe/acceptance/swebench/n75_ab_2026_11_06
echo $$ > $OUT/reps.pid
echo "reps start $(date "+%F %T") head=$(git rev-parse --short HEAD)" >> $OUT/STATUS
export PYTHONFAULTHANDLER=1 PYTHONUNBUFFERED=1 LUXE_LOG_TOOL_CALLS=1
SUB=benchmarks/swebench/subsets/v1_baseline_n75.json
for spec in rep2:control rep2:ctxonly rep3:ctxonly rep3:control; do
  rep=${spec%%:*}; arm=${spec##*:}
  case $arm in
    ctxonly) cfg=configs/single_64gb_swebench_ctxonly.yaml;;
    control) cfg=configs/single_64gb_swebench.yaml;;
  esac
  D=$OUT/$rep/$arm; mkdir -p $D/harness
  echo "$rep $arm run start $(date "+%F %T") cfg=$cfg" >> $OUT/STATUS
  .venv/bin/python -m benchmarks.swebench.run --config $cfg --subset $SUB \
    --per-instance-timeout 1800 --output $D/ > $OUT/$rep/$arm.log 2>&1
  echo "$rep $arm run exit=$? $(date "+%F %T")" >> $OUT/STATUS
  colima status >/dev/null 2>&1 || colima start >> $OUT/grade.log 2>&1
  export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
  ( cd $D/harness && PYTHONPATH=$HOME/Downloads/luxe ~/.venvs/swebench-grading/bin/python -u -c "
from pathlib import Path
from benchmarks.swebench.harness import run_harness, write_harness_summary
out = Path('.').resolve()
r = run_harness(Path('../predictions.json').resolve(), output_dir=out, run_id='n75_${rep}_${arm}')
write_harness_summary(r, out / 'harness_summary.json')
print('$rep $arm resolved', sum(x.resolved for x in r.values()), '/', len(r), 'errors', sum(bool(x.error) for x in r.values()))
" ) >> $OUT/grade.log 2>&1
  echo "$rep $arm grade exit=$? $(date "+%F %T")" >> $OUT/STATUS
  docker ps -a --format '{{.Names}}' | grep '^sweb.eval' | xargs -r docker rm -f >> $OUT/grade.log 2>&1
done
echo "REPS DONE $(date "+%F %T")" >> $OUT/STATUS
