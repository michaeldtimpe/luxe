#!/bin/zsh
# C10 — repeat_penalty 1.05 A/B, 8 fixtures × 2 cells. Single oMLX server.
# Re-run 2026-08-12: the original 2026-06-11 result was RETRACTED (knob never
# reached the wire — extra_body was dropped; see lessons.md). 252f12f sends
# vendor fields top-level, so this is the first live measurement.
set -uo pipefail
cd "$(dirname "$0")/.."
# API key resolves via luxe.secrets (Keychain OMLX_API_KEY / ~/.luxe/secrets.env) — never hardcode it here.
export LUXE_LOG_TOOL_CALLS=1
mkdir -p acceptance/c10_repeat_penalty_2026_08_12
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
  --output acceptance/c10_repeat_penalty_2026_08_12
rc=$?
/bin/ls -t ~/.luxe/runs | head -40 > acceptance/c10_repeat_penalty_2026_08_12/run_id_manifest.txt
echo "=== C10 done rc=$rc $(date) ==="
exit $rc
