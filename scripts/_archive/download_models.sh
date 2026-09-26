#!/usr/bin/env bash
set -euo pipefail

# Download MLX-optimized models for swarm pipeline benchmarks.
#
# All models are from mlx-community on HuggingFace — pre-quantized to 4-bit
# with mlx-lm, stored as .safetensors in MLX format. They are NOT GGUF and
# do NOT run through llama.cpp or Ollama — they require an MLX-native server
# (oMLX, mlx-lm.server, etc.).
#
# Usage:
#   ./scripts/download_models.sh              # Download all families
#   ./scripts/download_models.sh qwen         # Qwen only
#   ./scripts/download_models.sh deepseek     # DeepSeek only
#   ./scripts/download_models.sh all          # All families + tools
#   ./scripts/download_models.sh tools        # Analysis tools only

FAMILY="${1:-all}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_DIR/.venv"

# Activate venv if it exists (hf CLI lives there)
if [[ -f "$VENV_DIR/bin/activate" ]]; then
    source "$VENV_DIR/bin/activate"
fi

# Locate the hf CLI
HF_CMD=""
if command -v hf &>/dev/null; then
    HF_CMD="hf"
elif [[ -x "$VENV_DIR/bin/hf" ]]; then
    HF_CMD="$VENV_DIR/bin/hf"
elif command -v huggingface-cli &>/dev/null; then
    HF_CMD="huggingface-cli"
fi

if [[ -z "$HF_CMD" ]]; then
    echo "ERROR: Neither 'hf' nor 'huggingface-cli' found."
    echo "Install with: pip install 'huggingface_hub[cli]'"
    exit 1
fi

echo "Using download tool: $HF_CMD"

download() {
    local repo="$1"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Downloading: $repo"
    echo "  Format: MLX 4-bit quantized (.safetensors)"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    $HF_CMD download "$repo" 2>&1 || {
        echo "  WARNING: Failed to download $repo — skipping"
    }
}

# ── Qwen Family (32 GB) ──────────────────────────────────────────────
# All Qwen2.5 models have native tool/function calling support.
# MLX weights are pre-quantized by mlx-community with mlx-lm.
download_qwen() {
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  QWEN FAMILY — 4 unique MLX models, ~21 GB total           ║"
    echo "║  All models: MLX 4-bit quantized, native tool calling       ║"
    echo "╚══════════════════════════════════════════════════════════════╝"

    download "mlx-community/Qwen2.5-3B-Instruct-4bit"           # ~1.62 GB — architect, validator
    download "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"     # ~3.99 GB — worker_read, worker_analyze
    download "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"    # ~7.74 GB — worker_code
    download "mlx-community/Qwen2.5-14B-Instruct-4bit"          # ~7.74 GB — synthesizer

    echo ""
    echo "✓ Qwen family download complete"
    echo "  Peak single-model memory: ~7.74 GB (Coder-14B or 14B-Instruct)"
}

# ── DeepSeek Family (32 GB) ──────────────────────────────────────────
# R1-Distill-Qwen models inherit Qwen tool calling.
# Coder-V2-Lite is MoE (16B total / 2.4B active) — relies on text-recovery
# for tool calls (no native template).
download_deepseek() {
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  DEEPSEEK FAMILY — 5 unique MLX models, ~39 GB total       ║"
    echo "║  R1-Distill: MLX 4-bit, tool calling via Qwen template     ║"
    echo "║  Coder-V2-Lite: MLX 4-bit MoE, text-based tool calls      ║"
    echo "╚══════════════════════════════════════════════════════════════╝"

    download "mlx-community/DeepSeek-R1-Distill-Qwen-1.5B-4bit"            # ~0.93 GB — architect, validator
    download "mlx-community/DeepSeek-Coder-V2-Lite-Instruct-4bit-mlx"      # ~8.23 GB — worker_read (MoE)
    download "mlx-community/DeepSeek-R1-Distill-Qwen-7B-4bit"              # ~3.99 GB — worker_analyze
    download "mlx-community/DeepSeek-R1-Distill-Qwen-14B-4bit"             # ~7.74 GB — worker_code
    download "mlx-community/DeepSeek-R1-Distill-Qwen-32B-4bit"             # ~17.17 GB — synthesizer

    echo ""
    echo "✓ DeepSeek family download complete"
    echo "  Peak single-model memory: ~17.17 GB (R1-Distill-Qwen-32B)"
}

# ── Install tools ─────────────────────────────────────────────────────
install_tools() {
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  CHECKING / INSTALLING REQUIRED TOOLS                       ║"
    echo "╚══════════════════════════════════════════════════════════════╝"

    # Python analysis tools
    pip install ruff mypy bandit pip-audit 2>/dev/null || echo "  Note: Some Python tools may need manual install"

    # Check for ripgrep
    if ! command -v rg &>/dev/null; then
        echo "  WARNING: ripgrep (rg) not found. Install: brew install ripgrep"
    else
        echo "  ✓ ripgrep $(rg --version | head -1)"
    fi

    # Check for git
    if ! command -v git &>/dev/null; then
        echo "  WARNING: git not found"
    else
        echo "  ✓ $(git --version)"
    fi

    echo ""
    echo "✓ Tool check complete"
}

# ── Main ──────────────────────────────────────────────────────────────
echo "Swarm Pipeline — Model Download (MLX format)"
echo "=============================================="
echo ""

case "$FAMILY" in
    qwen)
        download_qwen
        ;;
    deepseek)
        download_deepseek
        ;;
    all)
        download_qwen
        download_deepseek
        install_tools
        ;;
    tools)
        install_tools
        ;;
    *)
        echo "Usage: $0 [qwen|deepseek|all|tools]"
        exit 1
        ;;
esac

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Done. Start oMLX and verify with: swarm check --config <path>"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
