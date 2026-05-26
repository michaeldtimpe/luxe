# Proposed `.sdd` and `CLAUDE.md` additions (drift fixes D2, D3)

These are textual patches to apply during an m5-quiet window. Each is small
enough to apply by hand without a unified diff; the exact insertion point is
given. **No files have been modified in the luxe tree.**

---

## Fix for D2 — pin the champion model in `src/luxe/luxe.sdd`

**Why:** the positive statement *"use `Qwen3.6-35B-A3B-6bit`"* currently lives
only in `CLAUDE.md:8` and `configs/single_64gb.yaml`. The `.sdd` chain only
encodes *exclusions* (no Instruct-2507, no dense >30B mxfp8). Add a positive
Must so the most load-bearing policy in the project has tool-side legibility.

**Insertion:** add the two new bullets to the `## Must` section of
`src/luxe/luxe.sdd`. Recommended insertion point is right after the existing
`Mono-only execution mode` and `temp=0.0` lines, keeping the highest-level
invariants together.

```diff
 ## Must
 - Mono-only execution mode (single capable model, full tool surface)
 - temp=0.0 in production fixture configs
+- Pin `Qwen3.6-35B-A3B-6bit` as the single champion MoE model (configured in
+  `configs/single_64gb.yaml`); the m5max_moe bake-off (2026-05-10) is settled
+- No per-task model selection, model fan-out, or router; re-bench only on
+  explicit user request and write results under `acceptance/m5max_moe_<id>/`
 - All mono prompt edits go through `src/luxe/agents/prompts.py` registry
 - Pin `--work-dir ~/.luxe/bench-workspace` for bench runs (random tempdir leaks into prompts)
 - `oMLX` `idle_timeout_seconds: 1800` (null keeps models resident forever)
 - `--keep-loaded` for bench mode (post-run unload fires by default)
```

**Optional:** if you want a corresponding negative as well, add to `## Must not`:

```diff
 ## Must not
 - Read `origin/<branch>` in offline-cache repos (stale-ref trap; use local ref or sidecar regrade)
 - Use MoE Instruct-2507 model family (long-context fabrication, skips optional tool calls)
+- Promote configs from `configs/_archive/` (reference-only; not the champion)
 - Use `--no-verify`, `--no-gpg-sign`, or other hook-skip flags
 - Force-push to `main` or `master`
 - Configure dense >30B mxfp8 models (don't fit on 64GB Mac under load)
```

After applying, the redundancy with `CLAUDE.md:6-33` is intentional — the
`.sdd` is the contract, `CLAUDE.md` is the onboarding restatement.

---

## Fix for D3 — surface opt-in modes from `CLAUDE.md`

**Why:** the reflect-stage (Track 1, pinned 2026-05-24) and v1.11 adaptive-
policy invariants live only in `src/luxe/agents/agents.sdd`. They're opt-in
and default-off, so the absence of a `CLAUDE.md` pointer isn't a
contradiction — but a user toggling `LUXE_REFLECT=1` or
`LUXE_ADAPTIVE_POLICY=1` from `CLAUDE.md`'s guidance alone never sees the
invariants that protect those modes.

**Insertion:** add a new section to `CLAUDE.md`, recommended after
`## Architecture: SpecDD Lever 2 .sdd chain` (around line 45) and before
`## When working on this repo`. Keeps the "chain → opt-in modes → working
norms" ordering coherent.

```markdown
## Opt-in modes (default off, byte-identical when disabled)

Three subsystems are gated by env vars and default to **off**. Each has
invariants in its `.sdd` you must read before enabling:

- **Reflect / verify stage** (`LUXE_REFLECT=1`) — a separate `backend.chat`
  critique pass. Verify-only by default (non-perturbing). See
  `src/luxe/agents/agents.sdd` § "Reflection / verify stage invariants".
- **Adaptive policy** (`LUXE_ADAPTIVE_POLICY=1`) — convergence-score-based
  intervention-intensity modulation. **Bias-not-lock**: never gates dispatch.
  Slew-rate limited via `LUXE_ADAPTIVE_MAX_INTENSITY_DELTA_PER_STEP`. See
  `agents.sdd` § "Stage 3 / v1.11 adaptive-policy invariants".
- **Cohort priors** (`LUXE_LOAD_PRIORS=1`) — reads
  `~/.luxe/cohort-history/<instance>.json`. **Log-only in v1.11** (does not
  influence intervention intensity); promotion deferred to v1.11.1+.

If you toggle any of these on, walk the relevant `.sdd` section first —
unbiased flips can silently change benchmark behavior.
```
