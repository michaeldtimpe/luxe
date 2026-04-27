#!/usr/bin/env bash
# Unattended catch-up for an existing overnight_<TS> run. Re-runs only
# the slots that produced no usable data the first time:
#
#   1. synthetic_baseline patch:  qwen2.5-32b-instruct × llamacpp × prefix_cache_decay
#   2. multi_turn_reviews × {elara, never-say-yes, neon-rain} × omlx
#   3. verdicts (re-run to absorb the new data)
#
# LM Studio sub-chunks were dropped 2026-04-27 — the Qwen 32B tool-loop
# bug is downstream-fixable only.
#
# Usage:
#   export OMLX_API_KEY=omlx-...
#   TS=overnight_<timestamp> nohup bash scripts/run_overnight_catchup.sh > luxe/catchup.log 2>&1 &
#   tail -f luxe/catchup.log
#
# Estimated wall: 2-5 hours. Each step's stdout is captured in the
# overnight results dir AND echoed to the catchup log. A failure in
# any step does not abort the sequence — the next step starts anyway.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

PY=.venv/bin/python
TS="${TS:-overnight_2026-04-26T11-46-44}"
RESULTS_DIR="results/$TS"

[ -x "$PY" ] || { echo "missing $PY — run 'uv sync' first"; exit 1; }
[ -d "$RESULTS_DIR" ] || { echo "missing $RESULTS_DIR"; exit 1; }
[ -n "${OMLX_API_KEY:-}" ] || echo "[warn] OMLX_API_KEY unset — oMLX steps will fail"

stamp() { date -u "+%Y-%m-%dT%H:%M:%SZ"; }

step() {
    local label="$1"; shift
    echo
    echo "================================================================"
    echo "[$(stamp)] step: $label"
    echo "================================================================"
    "$@" || echo "[$(stamp)] $label exited non-zero ($?) — continuing"
}

# ── 1. patch the missing synthetic_baseline slot ────────────────────
step "synthetic_baseline patch (32b × llamacpp × prefix_cache_decay)" \
    "$PY" scripts/run_ab_benchmark.py \
        --candidate qwen2.5-32b-instruct \
        --backends llamacpp \
        --bench prefix_cache_decay \
        --phase "$TS" \
        --limit 30

# ── 2. multi_turn_reviews × omlx (3 repos) ──────────────────────────
for repo in elara never-say-yes neon-rain; do
    step "multi_turn_reviews ${repo} × omlx" \
        "$PY" scripts/run_overnight.py \
            --resume "$TS" --only multi_turn_reviews \
            --repo "$repo" --backend omlx
done

# ── 3. re-run verdicts on the now-richer dataset ────────────────────
step "verdicts" \
    "$PY" scripts/run_overnight.py --resume "$TS" --only verdicts

# ── final summary ───────────────────────────────────────────────────
echo
echo "================================================================"
echo "[$(stamp)] catchup complete"
echo "================================================================"
echo
echo "Reports:"
echo "  $RESULTS_DIR/VERDICT.md"
echo "  $RESULTS_DIR/SPEC_DECODING_VERDICT.md"
echo "  $RESULTS_DIR/COMPOSITE_VERDICT.md"
echo
echo "Multi-turn task records (all 6 sub-chunks span ~/.luxe/tasks/T-*):"
ls -1 "$HOME/.luxe/tasks/" | grep "^T-2026" | tail -15
