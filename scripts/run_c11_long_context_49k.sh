#!/bin/zsh
# C11 — BFCL multi_turn_long_context capability-curve point at num_ctx=49152.
# Documented context-limited floor: 39.0% at 32K (M1); 57.5% at 131K (M5
# faithful, post-4b5d462 grader). 49K is the largest window that fits the M1
# 64GB host (champion 28.4GB + KV under the 36GB wired cap). n=200 single rep
# is fine for a curve point. Escalate to 65536 ONLY if 49K is still clearly
# context-limited. Single oMLX server — never overlap with other model work.
set -uo pipefail
cd "$(dirname "$0")/.."
export OMLX_API_KEY=omlx-sdb25582k3mq8pf9
mkdir -p acceptance/bfcl/multi_turn_long_context/m1_49k
echo "=== C11 start $(date) ==="
PYTHONUNBUFFERED=1 .venv/bin/python -m benchmarks.bfcl.run \
  --categories multi_turn_long_context \
  --num-ctx 49152 --temperature 0 \
  --model qwen3.6-35b-a3b-6bit \
  --base-url http://127.0.0.1:8000 \
  --output acceptance/bfcl/multi_turn_long_context/m1_49k/
rc=$?
echo "=== C11 done rc=$rc $(date) ==="
exit $rc
