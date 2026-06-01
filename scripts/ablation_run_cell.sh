#!/usr/bin/env bash
# Run one forge-hybrid ablation cell across BFCL (agentic), maintain_suite,
# and SWE-bench (preds + Docker harness).
#
# Cells (the 4 the user picked for the 2026-05-31 → forge-hybrid refresh):
#   baseline     — all forge-hybrid features OFF (closest agentic equivalent
#                  to the 2026-05-27 raw-mode baseline)
#   tiered       — current deployed default (TieredCompact ON, others OFF)
#   respond      — tiered + LUXE_RESPOND_TERMINAL=1 (Phase 3 B at scale,
#                  retest the 0/14 smoke refutation)
#   trajectory   — tiered + LUXE_EARLY_BAIL_TRAJECTORY_SHAPE=1 (Phase 4 D
#                  at scale, requires LUXE_ADAPTIVE_POLICY=1 dependency)
#
# Output layout (all paths gitignored by default; promote-to-research-repo
# happens via scripts/ablation_aggregate.py at the end):
#   acceptance/agentic_ablation/<cell>/bfcl/
#   acceptance/agentic_ablation/<cell>/maintain_suite/
#   acceptance/agentic_ablation/<cell>/swebench/
#   acceptance/agentic_ablation/<cell>/swebench/harness/
#
# Resume-safe: per-item caching at every benchmark; re-invoking on the
# same output dir skips completed items. Crashes mid-run are recoverable
# by re-running the same command.
#
# Usage (run from luxe repo root):
#   bash scripts/ablation_run_cell.sh <cell> [options]
#
#   --bfcl-limit N         per-category BFCL cap (default: full corpus)
#   --bfcl-categories LIST comma-separated; default: simple_python,multiple,
#                          parallel,parallel_multiple,irrelevance
#   --swebench-smoke N     SWE-bench first-N (default: 50)
#   --skip-bfcl            don't run BFCL
#   --skip-maintain        don't run maintain_suite
#   --skip-swebench        don't run SWE-bench
#   --skip-harness         run SWE-bench preds only (no Docker scoring)
#   --harness-workers N    parallel Docker workers (default: 2)
#
# Per-cell wall (rough, sequential, no parallelism):
#   - BFCL agentic full (1640 problems) ~15-30h (TieredCompact may shave 30-50%)
#   - maintain_suite (10 fixtures)       ~1-2h
#   - SWE-bench preds n=50                ~2-3h
#   - SWE-bench Docker n=50               ~20-25h at max-workers=2
#                                         (parallelize with --harness-workers 4-8 if RAM allows)

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ $# -lt 1 ]]; then
  sed -n '2,50p' "$0"
  exit 2
fi

CELL="$1"
shift

# Defaults
BFCL_LIMIT=""
BFCL_CATEGORIES="simple_python,multiple,parallel,parallel_multiple,irrelevance"
SWE_SMOKE=50
RUN_BFCL=1
RUN_MAINTAIN=1
RUN_SWEBENCH=1
RUN_HARNESS=1
HARNESS_WORKERS=2

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bfcl-limit)         BFCL_LIMIT="--limit $2"; shift 2;;
    --bfcl-categories)    BFCL_CATEGORIES="$2"; shift 2;;
    --swebench-smoke)     SWE_SMOKE="$2"; shift 2;;
    --skip-bfcl)          RUN_BFCL=0; shift;;
    --skip-maintain)      RUN_MAINTAIN=0; shift;;
    --skip-swebench)      RUN_SWEBENCH=0; shift;;
    --skip-harness)       RUN_HARNESS=0; shift;;
    --harness-workers)    HARNESS_WORKERS="$2"; shift 2;;
    -h|--help)            sed -n '2,50p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

# --- Env-var configuration per cell ---------------------------------------
#
# Be EXPLICIT (set both 0 and 1) so we're insulated from default drift in
# loop.py. unset LUXE_* first to avoid leakage from caller's env.
for v in LUXE_TIERED_COMPACT LUXE_TIERED_COMPACT_PHASE_THRESHOLDS \
         LUXE_RESPOND_TERMINAL LUXE_EARLY_BAIL_TRAJECTORY_SHAPE \
         LUXE_ADAPTIVE_POLICY LUXE_EARLY_BAIL LUXE_WRITE_PRESSURE \
         LUXE_ACTION_DENSITY_GATE LUXE_CONVERGENCE_GATE LUXE_PROSE_BURST \
         LUXE_REFLECT LUXE_EARLY_BAIL_COMMIT_ONLY LUXE_EARLY_BAIL_MODE; do
  unset "$v" || true
done

case "$CELL" in
  baseline)
    export LUXE_TIERED_COMPACT=0
    export LUXE_RESPOND_TERMINAL=0
    export LUXE_EARLY_BAIL_TRAJECTORY_SHAPE=0
    export LUXE_ADAPTIVE_POLICY=0
    # leave LUXE_EARLY_BAIL / LUXE_WRITE_PRESSURE at the per-benchmark
    # defaults (SWE-bench: ON, BFCL/maintain: usually OFF or per-runner)
    ;;
  tiered)
    export LUXE_TIERED_COMPACT=1
    export LUXE_TIERED_COMPACT_PHASE_THRESHOLDS="0.50,0.85,0.95"
    export LUXE_RESPOND_TERMINAL=0
    export LUXE_EARLY_BAIL_TRAJECTORY_SHAPE=0
    export LUXE_ADAPTIVE_POLICY=0
    ;;
  respond)
    export LUXE_TIERED_COMPACT=1
    export LUXE_TIERED_COMPACT_PHASE_THRESHOLDS="0.50,0.85,0.95"
    export LUXE_RESPOND_TERMINAL=1
    export LUXE_EARLY_BAIL_TRAJECTORY_SHAPE=0
    export LUXE_ADAPTIVE_POLICY=0
    ;;
  trajectory)
    export LUXE_TIERED_COMPACT=1
    export LUXE_TIERED_COMPACT_PHASE_THRESHOLDS="0.50,0.85,0.95"
    export LUXE_RESPOND_TERMINAL=0
    export LUXE_EARLY_BAIL_TRAJECTORY_SHAPE=1
    # CLAUDE.md flags an implicit dependency on adaptive_policy for the
    # score_log population. Required for the suppression predicate to
    # actually fire.
    export LUXE_ADAPTIVE_POLICY=1
    # The suppression has nothing to suppress unless early_bail is on.
    export LUXE_EARLY_BAIL=1
    ;;
  *)
    echo "unknown cell: $CELL (expected: baseline|tiered|respond|trajectory)" >&2
    exit 2
    ;;
esac

OUT_ROOT="${ABLATION_OUT_ROOT:-acceptance/agentic_ablation}/${CELL}"
mkdir -p "$OUT_ROOT"
LOG_DIR="${OUT_ROOT}/_logs"
mkdir -p "$LOG_DIR"

echo "=== Ablation cell: ${CELL} ==="
echo "    LUXE_TIERED_COMPACT=${LUXE_TIERED_COMPACT:-unset}"
echo "    LUXE_TIERED_COMPACT_PHASE_THRESHOLDS=${LUXE_TIERED_COMPACT_PHASE_THRESHOLDS:-unset}"
echo "    LUXE_RESPOND_TERMINAL=${LUXE_RESPOND_TERMINAL:-unset}"
echo "    LUXE_EARLY_BAIL_TRAJECTORY_SHAPE=${LUXE_EARLY_BAIL_TRAJECTORY_SHAPE:-unset}"
echo "    LUXE_ADAPTIVE_POLICY=${LUXE_ADAPTIVE_POLICY:-unset}"
echo "    LUXE_EARLY_BAIL=${LUXE_EARLY_BAIL:-unset}"
echo "    output: ${OUT_ROOT}"
echo "    timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

# Activate venv if present (matches the rest of luxe's scripts).
if [[ -f .venv/bin/activate ]]; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
fi

# --- BFCL (agentic mode) ---
if [[ $RUN_BFCL -eq 1 ]]; then
  IFS=',' read -ra BFCL_CAT_ARR <<<"$BFCL_CATEGORIES"
  echo "--- BFCL agentic (categories: ${BFCL_CATEGORIES}) ---"
  set +e
  python -m benchmarks.bfcl.run \
    --categories "${BFCL_CAT_ARR[@]}" \
    --mode agent \
    --output "${OUT_ROOT}/bfcl" \
    $BFCL_LIMIT \
    2>&1 | tee "${LOG_DIR}/bfcl.log"
  b_rc=${PIPESTATUS[0]}
  set -e
  # Per-item caching means a re-run resumes; a crash here shouldn't skip the
  # rest of this cell's benchmarks (partial data over no data).
  [[ $b_rc -ne 0 ]] && echo "  note: BFCL exited rc=${b_rc} — continuing to remaining benchmarks"
fi

# --- maintain_suite ---
if [[ $RUN_MAINTAIN -eq 1 ]]; then
  echo "--- maintain_suite --all ---"
  set +e
  python -m benchmarks.maintain_suite.run --all \
    --output "${OUT_ROOT}/maintain_suite" \
    2>&1 | tee "${LOG_DIR}/maintain_suite.log"
  m_rc=${PIPESTATUS[0]}
  set -e
  # maintain_suite returns 1 whenever the v1_release_gate (>=8/10 passing) is
  # not met. For an ablation cell that is an EXPECTED, non-fatal outcome — we
  # measure, we don't gate — and summary.json is already written. Don't let it
  # abort the cell before SWE-bench runs.
  [[ $m_rc -ne 0 ]] && echo "  note: maintain_suite exited rc=${m_rc} (v1 gate unmet or fixture error) — continuing"
fi

# --- SWE-bench ---
if [[ $RUN_SWEBENCH -eq 1 ]]; then
  SWE_OUT="${OUT_ROOT}/swebench"
  echo "--- SWE-bench preds (smoke ${SWE_SMOKE}) ---"

  # SWE-bench runner accepts several LUXE_* features as CLI flags; we still
  # set the env vars for parity with BFCL/maintain. Map the cell to flags
  # when the runner expects them (so adapter.py:326-375 wiring matches).
  SWE_FLAGS=()
  case "$CELL" in
    baseline)
      SWE_FLAGS+=("--no-early-bail" "--no-action-density-gate" "--no-convergence-gate")
      ;;
    tiered|respond|trajectory)
      SWE_FLAGS+=("--tiered-compact" "--tiered-compact-phase-thresholds" "0.50,0.85,0.95")
      ;;
  esac
  case "$CELL" in
    respond)
      SWE_FLAGS+=("--respond-terminal")
      ;;
    trajectory)
      SWE_FLAGS+=("--early-bail-trajectory-shape")
      ;;
  esac

  set +e
  python -m benchmarks.swebench.run \
    --smoke "$SWE_SMOKE" \
    --output "$SWE_OUT" \
    "${SWE_FLAGS[@]}" \
    2>&1 | tee "${LOG_DIR}/swebench_preds.log"
  p_rc=${PIPESTATUS[0]}
  set -e
  [[ $p_rc -ne 0 ]] && echo "  note: SWE-bench preds exited rc=${p_rc} — continuing"

  if [[ $RUN_HARNESS -eq 1 && -f "${SWE_OUT}/predictions.json" ]]; then
    echo "--- SWE-bench Docker harness (workers=${HARNESS_WORKERS}) ---"
    set +e
    python scripts/ablation_harness.py \
      --predictions "${SWE_OUT}/predictions.json" \
      --output-dir "${SWE_OUT}/harness" \
      --run-id "ablation_${CELL}_swe_smoke${SWE_SMOKE}" \
      --max-workers "$HARNESS_WORKERS" \
      2>&1 | tee "${LOG_DIR}/swebench_harness.log"
    h_rc=${PIPESTATUS[0]}
    set -e
    [[ $h_rc -ne 0 ]] && echo "  note: SWE-bench harness exited rc=${h_rc} — preds preserved, scoring incomplete"
  fi
fi

echo ""
echo "=== Cell ${CELL} DONE: ${OUT_ROOT} ==="
echo "    finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
