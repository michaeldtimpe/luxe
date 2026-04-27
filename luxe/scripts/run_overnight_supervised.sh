#!/usr/bin/env bash
# Run the overnight phases one at a time, prompting between each.
# Captures the timestamp from preflight automatically; every later
# phase resumes into the same results/overnight_<TS>/ directory.
#
# Usage:
#   export OMLX_API_KEY=omlx-...
#   bash scripts/run_overnight_supervised.sh
#
# At each prompt: y = run, n = skip, q = quit. Skipped phases can be
# re-run later with:  .venv/bin/python scripts/run_overnight.py \
#       --resume overnight_<TS> --only <phase>

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

PY=.venv/bin/python
[ -x "$PY" ] || { echo "missing $PY — run 'uv sync' first"; exit 1; }

# ── env warnings (don't abort — preflight will surface real problems) ─
[ -z "${OMLX_API_KEY:-}" ] && \
    echo "[warn] OMLX_API_KEY unset — oMLX phases will be skipped"

# ── helpers ─────────────────────────────────────────────────────────
confirm() {
    # confirm "phase name" "estimated duration"
    local phase="$1" eta="$2" ans
    echo
    echo "──────────────────────────────────────────────────────────────"
    while true; do
        read -r -p "Run [$phase] (~$eta)? [y]es / [n]o (skip) / [q]uit: " ans </dev/tty
        case "$ans" in
            y|Y|yes) return 0 ;;
            n|N|no)  echo "[skip] $phase"; return 1 ;;
            q|Q|quit) echo "stopping at user request — resume later with --resume $TS"; exit 0 ;;
            *) echo "  please answer y, n, or q" ;;
        esac
    done
}

show_status() {
    local phase="$1"
    if command -v jq >/dev/null 2>&1; then
        jq -r ".phases.\"$phase\" | \"  status=\(.status)  wall_s=\(.wall_s // 0)  error=\(.error // \"-\")\"" \
            "results/$TS/state.json"
    else
        $PY -c "import json,sys; d=json.load(open('results/$TS/state.json'))['phases'].get('$phase',{}); \
                print(f\"  status={d.get('status')}  wall_s={d.get('wall_s',0)}  error={d.get('error','-')}\")"
    fi
}

run_phase() {
    # run_phase <phase_name> [extra args...]
    local phase="$1"; shift
    echo "[run] $phase  →  results/$TS/$phase.log"
    $PY scripts/run_overnight.py --resume "$TS" --only "$phase" "$@" \
        || echo "  (run_overnight exited non-zero — check log)"
    show_status "$phase"
}

# ── chunk 1: preflight (skipped if TS already set in env) ───────────
if [ -n "${TS:-}" ]; then
    echo "=== chunk 1/6: preflight — skipped (resuming TS=$TS) ==="
    [ -d "results/$TS" ] || { echo "results/$TS does not exist"; exit 1; }
else
    echo "=== chunk 1/6: preflight ==="
    preflight_out=$($PY scripts/run_overnight.py --only preflight 2>&1 | tee /dev/tty)
    TS=$(echo "$preflight_out" | grep -oE 'overnight_[0-9T-]+' | head -1)
    [ -z "$TS" ] && { echo "could not extract timestamp from preflight output"; exit 1; }
fi
echo
echo "phase id: $TS"
echo "results dir: results/$TS"
echo
echo "preflight backends:"
if command -v jq >/dev/null 2>&1; then
    jq '.checks' "results/$TS/preflight.json"
else
    $PY -c "import json; print(json.dumps(json.load(open('results/$TS/preflight.json'))['checks'], indent=2))"
fi

# ── chunk 2: synthetic_baseline ─────────────────────────────────────
echo
echo "=== chunk 2/6: synthetic_baseline ==="
confirm "synthetic_baseline" "≤90 min" && run_phase synthetic_baseline

# ── chunk 3: spec_decoding ──────────────────────────────────────────
echo
echo "=== chunk 3/6: spec_decoding ==="
confirm "spec_decoding" "≤60 min" && run_phase spec_decoding

# ── chunk 4: multi_turn_reviews — one (repo × backend) at a time ───
echo
echo "=== chunk 4/6: multi_turn_reviews (6 sub-chunks: 3 repos × 2 backends) ==="
echo "Each sub-chunk is its own /review run (~45–90 min). Skip ones you "
echo "don't need. Sub-chunks share the same multi_turn_reviews phase entry "
echo "in state.json — only the last one's status is kept (use the per-run "
echo "result list inside that entry to see each)."
for repo in elara never-say-yes neon-rain; do
    for backend in ollama omlx; do
        echo
        echo "--- multi_turn_reviews: ${repo} x ${backend} ---"
        if confirm "multi_turn_reviews ${repo} x ${backend}" "45-90 min"; then
            run_phase multi_turn_reviews --repo "${repo}" --backend "${backend}"
        fi
    done
done

# ── chunk 5: dflash_long_output ─────────────────────────────────────
echo
echo "=== chunk 5/6: dflash_long_output ==="
confirm "dflash_long_output" "≤90 min" && run_phase dflash_long_output

# ── chunk 6: verdicts ───────────────────────────────────────────────
echo
echo "=== chunk 6/6: verdicts ==="
confirm "verdicts" "<1 min" && run_phase verdicts

# ── final summary ───────────────────────────────────────────────────
echo
echo "=============================================================="
echo "All chunks attempted. Final state:"
if command -v jq >/dev/null 2>&1; then
    jq -r '.phases | to_entries[] | "  \(.key): \(.value.status)  wall_s=\(.value.wall_s // 0)"' \
        "results/$TS/state.json"
else
    $PY -c "import json; \
        d=json.load(open('results/$TS/state.json'))['phases']; \
        [print(f'  {k}: {v.get(\"status\")}  wall_s={v.get(\"wall_s\",0)}') for k,v in d.items()]"
fi
echo
echo "Reports (if verdicts ran):"
echo "  results/$TS/VERDICT.md"
echo "  results/$TS/SPEC_DECODING_VERDICT.md"
echo "  results/$TS/COMPOSITE_VERDICT.md"
echo
echo "Resume any skipped phase later with:"
echo "  $PY scripts/run_overnight.py --resume $TS --only <phase>"
