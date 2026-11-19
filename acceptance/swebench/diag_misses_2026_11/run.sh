#!/bin/zsh
# SWE-bench diagnosis rep (2026-11): one control rep over the champion-miss subset.
# LUXE_SAVE_MESSAGES is exported by the swebench runner by default (adapter.py).
cd ~/Downloads/luxe-bench
OUT=$HOME/Downloads/luxe/acceptance/swebench/diag_misses_2026_11
mkdir -p $OUT
echo $$ > $OUT/runner.pid
export PYTHONFAULTHANDLER=1 PYTHONUNBUFFERED=1 LUXE_LOG_TOOL_CALLS=1
SUB=$HOME/Downloads/luxe/benchmarks/swebench/subsets/diag_misses_2026_11.json
arm=control
cfg=configs/single_64gb_swebench.yaml
echo "REP 1 ARM $arm start $(date "+%F %T")" >> $OUT/arms.log
.venv/bin/python -m benchmarks.swebench.run --config $cfg --subset $SUB \
  --per-instance-timeout 1800 --output $OUT/$arm/ > $OUT/$arm.log 2>&1
rc=$?
colima status >/dev/null 2>&1 || colima start >> $OUT/grade.log 2>&1
export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
mkdir -p $OUT/$arm/harness
( cd $OUT/$arm/harness && PYTHONPATH=$HOME/Downloads/luxe ~/.venvs/swebench-grading/bin/python -u -c "
from pathlib import Path
from benchmarks.swebench.harness import run_harness, write_harness_summary
out = Path('.').resolve()
r = run_harness(Path('../predictions.json').resolve(), output_dir=out, run_id='diag_$arm')
write_harness_summary(r, out / 'harness_summary.json')
print('$arm resolved', sum(x.resolved for x in r.values()), '/', len(r), 'errors', sum(bool(x.error) for x in r.values()))
" ) >> $OUT/grade.log 2>&1
echo "REP 1 ARM $arm end $(date "+%F %T") rc=$rc" >> $OUT/arms.log
echo "DONE diag $(date "+%F %T")" >> $OUT/arms.log
