# `_luxe_*` marker audit (forge-hybrid cycle Phase 1 exit criterion)

**Status**: complete (2026-05-26). Phase 1 (C) refactor + Phase 2 (A) compaction can
proceed against the findings below.

## Goal

Identify every `_luxe_*`-prefixed message-metadata marker any guard reads or writes,
classify load-bearing vs decorative, and surface design constraints for Axis A
(TieredCompact) compaction's "Phase-1 drop nudges" rule.

## Method

`grep -rn "_luxe_" src/luxe/ benchmarks/ tests/` over the whole tree, then triage
hits into:
- **Message-metadata markers** (the keys of message dicts in `messages[]`)
- **Procedural / private-name uses** (function/variable identifiers with
  `_luxe_` prefix — not markers)

## Findings

### Message-metadata markers (the load-bearing set)

| Marker | Source(s) | Owner | Read by | Purpose |
|---|---|---|---|---|
| `_luxe_repair` | `benchmarks/bfcl/adapter.py:608`, `src/luxe/agents/reflect.py:331` | `reflect.py` (Phase 2 repair) | `reflect.repair_context_view` filter | Keep BFCL Phase-2 repair nudges OUT of subsequent verify contexts (codified in `agents.sdd` lines 39 + 52). |

**Total: 1 marker.** Only the BFCL reflect/repair path tags messages.

### Procedural / private-name uses (not markers)

`benchmarks/maintain_suite/run.py` uses `_luxe_run_dir`, `_luxe_completed_stages`,
`_luxe_pipeline_complete`, `_luxe_pr_complete`, `_luxe_maintain`,
`_ensure_luxe_importable`. These are **private helper function names**, not message
markers. Compaction is irrelevant to them.

`benchmarks/swebench/adapter.py:209` defines `invoke_luxe_maintain` — same: private
function name, not a marker.

### Critical finding: SWE-bench loop has ZERO message-level guard markers today

All 7 SWE-bench guard injection points in `src/luxe/agents/loop.py` write plain
`{"role": "user", "content": <text>}` dicts with **no marker**:

| Line | Guard | Body source |
|---|---|---|
| 762 | `write_pressure` | `_WRITE_PRESSURE_MESSAGE` |
| 905 | `early_bail` (breadth_probe variant) | `_EARLY_BAIL_MESSAGE_BREADTH_PROBE` |
| 959 | `early_bail` (main variants) | `_EARLY_BAIL_MESSAGE_*` (default / soft_anchor / commit_imperative / no_abstain) |
| 1044 | `action_density_gate` | `_ACTION_DENSITY_GATE_MESSAGE` |
| 1100 | `prose_burst` | `_PROSE_BURST_MESSAGE` |
| 1213 | `spec_reprompt` (mid-loop) | `rr.detail` (SpecDD result) |
| 1277 | `spec_reprompt` (loop-break) | `rr.detail` (SpecDD result) |

## Design implications

### For Axis A (TieredCompact)

Forge's compaction Phase-1 logic identifies "nudges" by marker tag. **Luxe's
SWE-bench loop has no such tag today.** Two options:

1. **Body-text matching against `_EARLY_BAIL_MESSAGE_*` / `_WRITE_PRESSURE_MESSAGE`
   etc.** — fragile (string drift as variants are tuned; multi-instance match
   ambiguity).
2. **Add `_luxe_nudge=True` markers in Phase 1 (C) — recommended.** Backend.chat
   serializer strips local-only keys before wire serialization (so the marker is
   model-invisible). Each guard's `record()` method tags the injected message
   with `{"_luxe_nudge": True, "_luxe_nudge_type": "<guard_name>"}`. Phase 1 (C)
   refactor remains byte-identical on the WIRE (model sees the same tokens); only
   the local state dict gains keys. Replay-equivalence invariants 2–5 (termination
   reason, tool-call count, write count, convergence_score trajectory) are
   unaffected.

**Locked design constraint:** Phase 1 (C) refactor adds the `_luxe_nudge` /
`_luxe_nudge_type` keys at injection sites. Phase 2 (A) TieredCompact reads
those keys for Phase-1 drop-by-marker. Body-text matching is FORBIDDEN.

### For Axis A wire-byte-identity

The marker is local-only — never serialized to backend.chat. The plan's
"byte-identical baseline" invariant for Phase 1 (C) means **wire payload byte-
identical** (the model sees the same prompt), not "Python dict byte-identical."
The 5-invariant replay test must compare the wire-serialized messages, not the
raw in-process dicts. `tests/test_guardrails_refactor.py` must operationalize
this distinction.

### For Axis A protected messages

Compaction must never drop `messages[0:2]` (system prompt + task prompt) per
plan. Additionally:
- Must never drop messages tagged `_luxe_repair` (per `agents.sdd` filter
  rule — they must remain in the loop context even if dropped from verify
  contexts).
- May drop messages tagged `_luxe_nudge=True` at compaction Phase 1.

## Files touched by this audit

None — this is a read-only inventory. The design implications listed above
become Phase 1 (C) implementation requirements; they will be codified in
`agents.sdd` invariants during that phase.
