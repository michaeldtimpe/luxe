#!/usr/bin/env bash
# Launch the winning model in the optimized (all-on) configuration.
# Override with env vars: MODEL, DRAFT_MODEL, PORT, KV_BITS, DRAFT_TOKENS, CTX.

set -euo pipefail

MODEL="${MODEL:-mlx-community/Qwen2.5-Coder-32B-Instruct-4bit}"
DRAFT_MODEL="${DRAFT_MODEL:-mlx-community/Qwen2.5-Coder-1.5B-Instruct-4bit}"
PORT="${PORT:-8088}"
KV_BITS="${KV_BITS:-8}"
DRAFT_TOKENS="${DRAFT_TOKENS:-3}"
CTX="${CTX:-65536}"

cd "$(dirname "$0")/.."

exec uv run python -m mlx_lm.server \
    --host 127.0.0.1 \
    --port "$PORT" \
    --model "$MODEL" \
    --draft-model "$DRAFT_MODEL" \
    --num-draft-tokens "$DRAFT_TOKENS" \
    --kv-bits "$KV_BITS" \
    --kv-group-size 64 \
    --max-tokens "$CTX"
