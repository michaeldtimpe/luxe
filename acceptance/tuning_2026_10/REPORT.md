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

## Harness levers (2026-10-17, opt-in, 2f34af7 + fix 166c319)

User request: "try the cleanup turn harness change on Qwen3.8 and the
repeat-read for glimmer." Both flags are default-OFF; golden trajectories
are unchanged. 3 reps each, 60-min cap. Grades:
[HAND-VERIFY.md](HAND-VERIFY.md#harness-levers-2f34af7--fix-166c319).

6. **Cleanup turn (`LUXE_CLEANUP_TURN=1`) does what it was built for.**
   - Qwen3.8 healthcheck: 0/3 → **3/3 REAL**. Typing: REAL* (litter) →
     **3/3 REAL, clean**.
   - In 4 of 6 runs it offered exactly the model's own scratch files
     (`health_check_tmp.py`, `test_smoke_typing.py`, pyc) and the model
     answered DELETE for each. In the other runs Qwen3.8 left no litter and
     nothing was offered.
   - 0 legitimate files were offered in any run. The new
     `KeyboardInputHandler.js` is imported, so it was correctly never a
     candidate.
   - Cost: one tool-less call (a few seconds).
   - The first attempt was invalid: every cleanup turn raised on a `str`
     repo (`q38_cleanup_bug/`). Fixed and re-run.
   - **Caveat: neon reset went 1/3 REAL (2 BROKEN), against 3/3 in
     q38full.** The cleanup turn had 0 candidates in all three neon runs and
     runs after the agent loop, so it cannot have changed those
     trajectories. These are Qwen3.8 defects the earlier 3 reps didn't
     surface:
     - a method bound but never defined, with the first edit at step 29/30
       before the max-steps abort, so the partial edit was committed;
     - a call to a nonexistent `_restart()`.

     Both printed PASS because `npm test` never constructs the handler.
     Qwen3.8 on this arm: 7/9 REAL vs 6/9 baseline on the same fixtures.
   - **Status: candidate, not promoted.** A default-ON flip would need the
     champion no-harm 10×3. It is cheap and model-agnostic, since it fires
     only when a new unreferenced code file exists.
7. **Repeat-read stub (`LUXE_REPEAT_READ_STUB=1`) is near-inert on
   Glimmer.**
   - 2/6 REAL: neon 1/3, doc-config 1/3. Baseline was neon 0/10, doc-config
     2/4.
   - It fired once in 6 runs, and not in either pass, so the passes are
     variance.
   - Why: Glimmer's loop re-reads come after compaction has rewritten the
     earlier result (5–6 `read_after_compact` per failing run). The stub
     declines by design unless the earlier result is intact.
   - A variant that names the compacted earlier read is the next idea. It is
     not built.
   - **Status: inert for this model, not promoted.**
8. **Harness gap: step-limited runs shipped as finished. FIXED 2026-10-18
   (0f04e9a).**
   - A run that aborts at max steps used to commit its partial edit like a
     finished one (neon rep2 printed PASS on a constructor that throws).
   - Across m5's corpus, 111 runs ended at max steps with a diff: ~4 of ~600
     champion runs, and frequently on challengers (GLM 12/75, Laguna 12/39).
   - Now labelled, default ON (no model call): report and commit-body note,
     a draft PR never auto-promoted on green CI, a `step_limit_incomplete`
     event, and a bench ⚠ on the row.
   - Opt-in `LUXE_STEP_LIMIT_FINISH=1` adds one ≤8-step pass that shows the
     model its diff.
   - E2E on m5 with the champion at `max_steps: 6` (`steplimit_e2e/`):
     - labelling fired on both diff-producing runs and not on the no-diff
       run;
     - the finish pass ended on its own on shuffle (label cleared);
     - on healthcheck the finish pass used all 8 steps (label kept).
   - The finish pass is unbenched for help.

9. **Cleanup turn — champion no-harm passed (10×3, `champ_cleanup/`).**
   - 0 candidates in 30 runs, so the turn never reached the model and the
     champion's trajectories are untouched.
   - Hand-verified 27 REAL / 3 THIN. All 3 THIN are doc-config, which is the
     champion's own variance (rep2 is byte-identical to r2's THIN rep2).
   - 25/30 diffs are byte-identical to hand-verified r2 diffs.
   - **Eligible for default-ON on the user's call.** Evidence for help is
     Qwen3.8 only (healthcheck 0/3 → 3/3, typing litter removed); evidence
     for no harm is the champion 30 + Qwen3.8 9.
10. **Re-read note (`LUXE_REREAD_NOTE=1`, 60fb596) — refuted on Glimmer.**
    - The line-coverage key does catch Glimmer's loop: 10–12 notes per
      failing run, against 1 stub firing in 6 runs.
    - Glimmer re-reads the same lines right after each note.
    - 2/6 REAL (neon 1/3, doc-config 1/3), the same as the stub arm.
    - With the prompt appendix, the write-pressure notice, the exact-repeat
      stub and the line-coverage note all ignored, **Glimmer is closed**: no
      in-loop text lever moves it. Not promoted; the flag stays opt-in.

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
| `glim_stub/rep{1,2,3}/` | Glimmer-6bit + repeat-read stub × {neon reset, doc-config} |
| `q38_cleanup/rep{1,2,3}/`, `rep1_neon/` | Qwen3.8 + cleanup turn × {healthcheck, typing, neon reset} |
| `champ_cleanup/rep{1,2,3}/` | champion 10×3 + cleanup turn (no-harm) |
| `glim_reread/rep{1,2,3}/` | Glimmer-6bit + re-read note × {neon reset, doc-config} |
| `steplimit_e2e/{label,finish}/` | champion at max_steps 6 — step-limit labelling + finish-pass E2E |
| `q38_cleanup_bug/` | invalid first attempt (cleanup turn raised; kept for the record) |
