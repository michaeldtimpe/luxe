# tuning_2026_10 — can prompt tuning convert the newer challengers?

User request (2026-10-17, after m5max_moe_r2): understand Qwen3.8-27B and
muse-glimmer-30B better; see whether luxe can be tuned for them. All runs on
m5 (M5 Max 128 GB, oMLX 0.7.0), shipped defaults. Per-run grades:
[HAND-VERIFY.md](HAND-VERIFY.md).

## Verdicts

1. **`commit_first` (opt-in system appendix, f845455) does nothing measurable.**
   - **Glimmer:** stays 0/2. Its trace is the same as on the baseline prompt.
     The system-prompt instruction and the in-loop write-pressure notice are
     both ignored.
   - **Qwen3.8:** passes both dev fixtures with or without the appendix.
   - Not promoted, and not iterated further. It stays registered (opt-in,
     inert by default).
2. **Glimmer's defect is behavioural, not a prompting gap.** It plans the
   correct fix, then re-litigates pre-existing behaviour and re-reads the same
   region until max steps. This matches the gitkit finding (conclusion can't
   be prompt-disciplined). Any remaining lever is in the harness, e.g. a short
   reply to an identical repeat read. That would be opt-in, and is untested.
3. **Qwen3.8-27B-8bit on today's harness: 24/30 REAL (8/8/8 per rep).** It
   misses the ≥9 bar on two recurring defects:
   - nothing-ever-happens-document-config: 0/3 REAL (2 no-diff, 1 THIN). The
     August finding stands at 8-bit.
   - isomer-implement-healthcheck: 3/3 identical. The route is correct, but it
     leaves its throwaway verification script behind (`orphan_file` gate).
4. **Cost.**
   - Wall: avg 439–579 s per fixture, against 48 s for the champion and 181 s
     for the 27B-6bit fallback (r2).
   - Decode is ~17 tok/s (dense 8-bit, bandwidth-bound), and it generates
     7–10k tokens per fixture.
   - The prefix cache serves 78% of its prompt tokens, so prefill is ~10% of
     time. Cache work cannot close the gap; a smaller quant or thinking-off
     would be the levers (each needs its own A/B).
5. **What it is good at.** Where it passes, the work is often better than the
   references:
   - richer advisory audits;
   - guarded keyboard handlers;
   - it writes verification scripts and runs them.

   That last habit is also what produces the healthcheck/typing litter. A
   harness that fed the `orphan_file` finding back for a cleanup turn would
   convert healthcheck. That is a candidate, not tested here.

No champion, fallback, or manifest change.

## Harness notes (failures that were not the model)

- 30-min per-fixture cap killed two Qwen3.8 runs at step 14/23 — throughput,
  not a stall. Re-run at 3600 s (dev1b, q38full).
- One oMLX `incomplete_tool_call` 200-error ended a run (terminal by design:
  delivered tool calls must not re-execute). Re-run.
- `diagrun.sh` gained `OV_SRC=` to trace a run with a variant overlay.

## Runs

| Dir | What |
|---|---|
| `dev1/` | Glimmer+cf, Qwen3.8, Qwen3.8+cf × {neon reset, doc-config}, 30-min cap |
| `dev1b/` | Qwen3.8, Qwen3.8+cf × same, 60-min cap |
| `q38full/rep{1,2,3}/` | Qwen3.8-27B-8bit baseline, all 10 fixtures |
