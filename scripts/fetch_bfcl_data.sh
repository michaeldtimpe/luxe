#!/usr/bin/env bash
# Fetch BFCL v4 problem + ground-truth JSON files from the upstream
# Gorilla repo into ~/.luxe/bfcl-data/. Replaces the runtime dependency
# on the `bfcl_eval` PyPI package, which pins tree_sitter==0.21.3 and is
# incompatible with luxe's tree_sitter_language_pack (v1.10.1 substrate).
#
# Idempotent: skips files already present unless --force is passed.
# Default target: ~/.luxe/bfcl-data/. Override via LUXE_BFCL_DATA_DIR.
#
# Upstream: https://github.com/ShishirPatil/gorilla
#   path: berkeley-function-call-leaderboard/bfcl_eval/data/

set -euo pipefail

UPSTREAM="https://raw.githubusercontent.com/ShishirPatil/gorilla/main/berkeley-function-call-leaderboard/bfcl_eval/data"
TARGET="${LUXE_BFCL_DATA_DIR:-$HOME/.luxe/bfcl-data}"
FORCE=0
if [[ "${1:-}" == "--force" ]]; then FORCE=1; fi

CATEGORIES=(simple_python multiple parallel parallel_multiple irrelevance multi_turn_base multi_turn_long_context)
# Ground-truth exists for all except irrelevance.
# multi_turn_* are the stateful categories (clean involved-class subset); their
# state-based eval lives vendored under benchmarks/bfcl/multi_turn/ along with the
# per-class func-doc tool specs (version-controlled in-repo, not fetched here).
# long_context reuses the vendored long_context.py extension data + the checker's
# long_context flag. (miss_func/miss_param need per-turn tool-withholding — pending.)
GT_CATEGORIES=(simple_python multiple parallel parallel_multiple multi_turn_base multi_turn_long_context)

mkdir -p "$TARGET/possible_answer"

fetch() {
    local rel="$1"
    local dst="$TARGET/$rel"
    if [[ -f "$dst" && "$FORCE" -eq 0 ]]; then
        printf '  SKIP   %s (exists)\n' "$rel"
        return 0
    fi
    printf '  FETCH  %s\n' "$rel"
    curl -fsSL "$UPSTREAM/$rel" -o "$dst"
}

echo "Target: $TARGET"
echo "Upstream: $UPSTREAM"
echo

for cat in "${CATEGORIES[@]}"; do
    fetch "BFCL_v4_${cat}.json"
done

for cat in "${GT_CATEGORIES[@]}"; do
    fetch "possible_answer/BFCL_v4_${cat}.json"
done

echo
echo "Done. Verify with:"
echo "  ls -lh $TARGET"
echo "  ls -lh $TARGET/possible_answer"
