#!/usr/bin/env bash
# Re-run the 3 LM Studio multi_turn /review sub-chunks with the loop-
# guard fix in place, then re-aggregate + re-verdict so the composite
# report includes proper LM Studio data.
#
# Each sub-chunk has its own 600-min budget (the harness alarm). The
# loop guard fires after 3 identical calls in a row, so a stuck
# subtask should now break free into either a different tool or
# synthesis instead of burning all 20 steps.
#
# Usage:
#   export OMLX_API_KEY=omlx-...
#   nohup bash scripts/run_lmstudio_recheck.sh > lmstudio_recheck.log 2>&1 &
#   tail -f lmstudio_recheck.log
#   # Or interactive:
#   bash scripts/run_lmstudio_recheck.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

PY=.venv/bin/python
TS="${TS:-overnight_2026-04-26T11-46-44}"
RESULTS_DIR="results/$TS"

[ -x "$PY" ] || { echo "missing $PY — run 'uv sync' first"; exit 1; }
[ -d "$RESULTS_DIR" ] || { echo "missing $RESULTS_DIR"; exit 1; }

stamp() { date -u "+%Y-%m-%dT%H:%M:%SZ"; }
step() {
    local label="$1"; shift
    echo
    echo "================================================================"
    echo "[$(stamp)] step: $label"
    echo "================================================================"
    "$@" || echo "[$(stamp)] $label exited non-zero ($?) — continuing"
}

for repo in elara never-say-yes neon-rain; do
    step "multi_turn_reviews ${repo} x lmstudio (loop-guard active)" \
        "$PY" scripts/run_overnight.py \
            --resume "$TS" --only multi_turn_reviews \
            --repo "$repo" --backend lmstudio
done

step "aggregate multi_turn task records" \
    "$PY" scripts/aggregate_multi_turn.py --phase "$TS"

step "verdicts (re-run on richer dataset)" \
    "$PY" scripts/run_overnight.py --resume "$TS" --only verdicts

echo
echo "================================================================"
echo "[$(stamp)] lmstudio recheck complete"
echo "================================================================"
echo
echo "Final reports:"
echo "  $RESULTS_DIR/COMPOSITE_VERDICT.md"
echo "  $RESULTS_DIR/VERDICT.md"
echo "  $RESULTS_DIR/SPEC_DECODING_VERDICT.md"
echo
echo "Per-(repo, backend) grid (post-fix):"
"$PY" scripts/aggregate_multi_turn.py --phase "$TS" 2>&1 | tail -25
