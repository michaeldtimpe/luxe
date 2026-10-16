#!/bin/bash
export PATH=/opt/homebrew/bin:$HOME/.local/bin:$PATH
cd ~/luxe-bench-tmp/cache-ab
D=acceptance/cache_ab_2026_10
run() { arm=$1; shift
  echo "ARM $arm start $(date "+%F %T")" >> $D/arms.log
  env "$@" uv run python -m benchmarks.maintain_suite.run --all \
    --variants $D/variants_3rep.yaml --output $D/$arm/ \
    --per-fixture-timeout 1800 --work-dir ~/.luxe/bench-workspace > $D/$arm.log 2>&1
  echo "ARM $arm end $(date "+%F %T") rc=$?" >> $D/arms.log
}
run base   LUXE_NOOP=1
run R      LUXE_REASONING_ROUNDTRIP=1
run C      LUXE_COMPACT_CACHE_FRIENDLY=1
run RC     LUXE_REASONING_ROUNDTRIP=1 LUXE_COMPACT_CACHE_FRIENDLY=1
echo DONE >> $D/arms.log
