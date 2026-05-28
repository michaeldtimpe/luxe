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
prefer_4bit_arg=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bench)        benches="$2"; shift 2;;
    --limit)        limit="--limit $2"; shift 2;;
    --no-mlx-prompt) prompt_for_oMLX=0; shift;;
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

echo "=== Eval suite ${ts} (benches: ${benches}) ==="

# --- Phase A: HTTP-Backend benchmarks ---
if has_bench gsm8k; then
  echo "--- GSM8K ---"
  python -m benchmarks.gsm8k.run --output "${out_root}/gsm8k" $limit
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
