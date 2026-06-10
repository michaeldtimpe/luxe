#!/bin/zsh
# C12 adaptive-policy smoke driver (2026-06-10).
# Two sequential arms on the single oMLX server — never parallel.
#   arm 1: baseline (LUXE_ADAPTIVE_POLICY unset -> disable-equivalence)
#   arm 2: LUXE_ADAPTIVE_POLICY=1
# 5 fixtures: 2 implement, 2 document, 1 manage.
set -uo pipefail
cd "$(dirname "$0")/.."

export OMLX_API_KEY=omlx-sdb25582k3mq8pf9
export LUXE_LOG_TOOL_CALLS=1

FIXTURES=(
  --id lpe-rope-calc-implement-strict-flag
  --id the-game-implement-shuffle-shortcut
  --id neon-rain-document-modules
  --id nothing-ever-happens-manage-deps-audit
  --id nothing-ever-happens-document-config
)
COMMON=(
  --variants benchmarks/maintain_suite/variants_adaptive_smoke.yaml
  --work-dir ~/.luxe/bench-workspace
  --per-fixture-timeout 1800
)

echo "=== C12 arm 1/2: baseline (adaptive OFF) $(date) ==="
unset LUXE_ADAPTIVE_POLICY
.venv/bin/python -m benchmarks.maintain_suite.run \
  "${FIXTURES[@]}" "${COMMON[@]}" \
  --output acceptance/adaptive_smoke/baseline
rc1=$?
echo "=== arm 1 rc=$rc1 $(date) ==="

# run_id manifest for arm 1 (feedback_save_run_id_manifest_after_every_bench)
ls -t ~/.luxe/runs | head -20 > acceptance/adaptive_smoke/baseline/run_id_manifest.txt

echo "=== C12 arm 2/2: LUXE_ADAPTIVE_POLICY=1 $(date) ==="
export LUXE_ADAPTIVE_POLICY=1
.venv/bin/python -m benchmarks.maintain_suite.run \
  "${FIXTURES[@]}" "${COMMON[@]}" \
  --output acceptance/adaptive_smoke/adaptive
rc2=$?
echo "=== arm 2 rc=$rc2 $(date) ==="
ls -t ~/.luxe/runs | head -40 > acceptance/adaptive_smoke/adaptive/run_id_manifest.txt

echo "=== C12 done: rc1=$rc1 rc2=$rc2 $(date) ==="
exit $(( rc1 || rc2 ))
