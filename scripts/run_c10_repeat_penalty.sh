#!/bin/zsh
# C10 — repeat_penalty 1.05 A/B, 8 fixtures × 2 cells. Single oMLX server.
set -uo pipefail
cd "$(dirname "$0")/.."
export OMLX_API_KEY=omlx-sdb25582k3mq8pf9
export LUXE_LOG_TOOL_CALLS=1
mkdir -p acceptance/c10_repeat_penalty
echo "=== C10 start $(date) ==="
PYTHONUNBUFFERED=1 .venv/bin/python -m benchmarks.maintain_suite.run \
  --id lpe-rope-calc-implement-strict-flag \
  --id the-game-implement-shuffle-shortcut \
  --id the-game-document-architecture \
  --id neon-rain-implement-reset-shortcut \
  --id neon-rain-document-modules \
  --id isomer-implement-healthcheck \
  --id nothing-ever-happens-manage-deps-audit \
  --id nothing-ever-happens-document-config \
  --variants benchmarks/maintain_suite/variants_c10_repeat_penalty.yaml \
  --work-dir ~/.luxe/bench-workspace \
  --per-fixture-timeout 1800 \
  --output acceptance/c10_repeat_penalty
rc=$?
/bin/ls -t ~/.luxe/runs | head -40 > acceptance/c10_repeat_penalty/run_id_manifest.txt
echo "=== C10 done rc=$rc $(date) ==="
exit $rc
