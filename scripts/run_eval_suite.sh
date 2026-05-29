#!/usr/bin/env bash
# Extended-benchmark suite runner. Sequences HTTP-Backend benchmarks
# (GSM8K, CodeNeedle — talk to oMLX over HTTP) before mlx_lm-direct
# benchmarks (MMLU, ARC, Perplexity — load model in-process).
#
# Memory note: the mlx_lm-direct phase loads the 35B 6-bit weights into
# this process (~25 GB). On the 64 GB machine, stop the oMLX server
# before that phase or accept a tight memory budget.
#
# Usage:
#   bash scripts/run_eval_suite.sh                       # all benchmarks
#   bash scripts/run_eval_suite.sh --bench gsm8k,mmlu    # subset (comma-list)
#   bash scripts/run_eval_suite.sh --limit 100           # quick partial pass
#   bash scripts/run_eval_suite.sh --no-mlx-prompt       # skip the oMLX-stop prompt
#   bash scripts/run_eval_suite.sh --mem-tier high       # 128+ GB host: keep oMLX up
#   bash scripts/run_eval_suite.sh --mem-tier low        # 64 GB host: force the prompt
#   bash scripts/run_eval_suite.sh --mem-tier auto       # detect from sysctl (default)
set -euo pipefail

cd "$(dirname "$0")/.."
source .venv/bin/activate

ts="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
out_root="acceptance/eval_suite/${ts}"
mkdir -p "$out_root"

# Defaults
benches="gsm8k,codeneedle,mmlu,arc,perplexity"
limit=""
prompt_for_oMLX=1
mem_tier="auto"   # auto | low | high
prefer_4bit_arg=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bench)        benches="$2"; shift 2;;
    --limit)        limit="--limit $2"; shift 2;;
    --no-mlx-prompt) prompt_for_oMLX=0; shift;;
    --mem-tier)     mem_tier="$2"; shift 2;;
    -h|--help)
      sed -n '2,18p' "$0"
      exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

# Comma-list → array
IFS=',' read -ra bench_array <<<"$benches"
has_bench() {
  local target="$1"
  for b in "${bench_array[@]}"; do
    [[ "$b" == "$target" ]] && return 0
  done
  return 1
}

# Memory-tier resolution.
#
# Phase B (mlx_direct) loads the 35B 6-bit weights into THIS process (~25 GB).
# When oMLX is still serving the same model, total RAM roughly doubles. On a
# 64 GB host that requires stopping oMLX first; a 128+ GB host can run both
# concurrently. Auto-detect on macOS via sysctl, with --mem-tier override.
ram_gb=$(($(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1024 / 1024 / 1024))
if [[ "$mem_tier" == "auto" ]]; then
  # Conservative: anything below 128 GB → low (requires oMLX stop).
  if [[ "$ram_gb" -ge 128 ]]; then mem_tier="high"; else mem_tier="low"; fi
fi
case "$mem_tier" in
  low)  ;;
  high) prompt_for_oMLX=0 ;;
  *)    echo "invalid --mem-tier: $mem_tier (expected: auto|low|high)" >&2; exit 2;;
esac

echo "=== Eval suite ${ts} (benches: ${benches}) ==="
echo "    mem-tier=${mem_tier}  host_ram=${ram_gb}GB  oMLX_prompt=${prompt_for_oMLX}"

# --- Phase A: HTTP-Backend benchmarks ---
if has_bench gsm8k; then
  # Run GSM8K twice:
  #   gsm8k_think    — deployed-mode (think=True, max_tokens=4096): how luxe
  #                    actually uses the model. Calibrates near 99%.
  #   gsm8k_nothink  — canonical Wei et al. 8-shot methodology (/no_think,
  #                    max_tokens=512): cross-comparable with public leaderboards.
  echo "--- GSM8K (think) ---"
  python -m benchmarks.gsm8k.run --output "${out_root}/gsm8k_think" --think --max-tokens 4096 $limit
  echo "--- GSM8K (no-think) ---"
  python -m benchmarks.gsm8k.run --output "${out_root}/gsm8k_nothink" --no-think --max-tokens 512 $limit
fi
if has_bench codeneedle; then
  echo "--- CodeNeedle ---"
  python -m benchmarks.codeneedle.run --output "${out_root}/codeneedle"
fi

# --- Phase B: mlx_lm-direct benchmarks (require oMLX-free memory) ---
phase_b=0
for b in mmlu arc perplexity; do
  has_bench "$b" && phase_b=1
done

if [[ $phase_b -eq 1 && $prompt_for_oMLX -eq 1 ]]; then
  cat <<'WARN'

>>> Phase B (mlx_lm in-process) about to start.
>>> The 35B model will be loaded into this process (~25 GB).
>>> If the oMLX server is still running with the same model loaded,
>>> total memory will be roughly double. Stop it now if needed,
>>> then press Enter to continue (or Ctrl-C to abort).

WARN
  read -r
elif [[ $phase_b -eq 1 && "$mem_tier" == "low" ]]; then
  # --no-mlx-prompt with low tier: auto-stop oMLX so Phase B has memory headroom.
  if command -v brew >/dev/null 2>&1 && brew services list 2>/dev/null | grep -q '^omlx .* started'; then
    echo ">>> mem-tier=low: stopping oMLX before Phase B (auto, --no-mlx-prompt)"
    brew services stop omlx >/dev/null || true
  fi
elif [[ $phase_b -eq 1 && "$mem_tier" == "high" ]]; then
  echo ">>> Phase B starting alongside oMLX (mem-tier=high, ${ram_gb}GB host)."
fi

if has_bench mmlu; then
  echo "--- MMLU ---"
  python -m benchmarks.mmlu.run --output "${out_root}/mmlu" $limit
fi
if has_bench arc; then
  echo "--- ARC-Challenge ---"
  python -m benchmarks.arc_challenge.run --output "${out_root}/arc_challenge" $limit
fi
if has_bench perplexity; then
  echo "--- Perplexity ---"
  python -m benchmarks.perplexity.run --output "${out_root}/perplexity"
fi

echo "--- aggregating ---"
python scripts/aggregate_eval_suite.py --run-dir "${out_root}"

echo
echo "=== DONE: ${out_root}/summary.md ==="
