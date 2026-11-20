# Champion document-config at 32/55 since 2026-11-19: diagnosis

**Verdict: a serving-stack change, not a luxe regression.** oMLX 0.8.0 (installed on m5
2026-11-18 08:31) resolves a step-0 near-tie differently from 0.7.x, and deterministically. The
run then takes a reading path that never reaches the `PM_NH_*` block. luxe's first request, the
weights and the chat template are unchanged. Forensics only, no model runs (the GPU was busy
with the image-exec slice).

## Evidence

1. **Where runs diverge: step 0, the model's first output.** Every champion run in
   idle_capped / abort_report / failed_output (21) starts `list_dir`, then a `bm25_search` that
   returns 226 bytes. Every October run starts the same way, but the search returns 381 bytes
   (`~/.luxe/runs/<id>/events.jsonl`, e.g. f19787bbcff3, b070c4728014 vs 304ecebc93f1,
   8240d27a4777). The November query is `"environment variables config startup"` (saved messages).
2. **Same input, different output.** Step 0 `prompt_tokens` is 2096 in all four October runs
   checked and in both November runs. `completion_tokens` is 173 in October and 168 in November.
   At temp 0 with an identical prompt, the difference is on the server.
3. **luxe's request did not change.** `tests/golden/run_single_request.json` (the pinned first
   request) has no change between 800daf5 (2026-10-17) and ced100e (the November runs' code). The
   golden changes in that window are trajectory-only or removed variants.
4. **Weights and template did not change.** The model's HF snapshot (`cb7e092e`) files are all
   dated 2026-05-09. The chat template is unchanged.
5. **The server did change.** Reports record oMLX 0.7.0 (October: tuning, m5max_moe_r2, cache_ab),
   0.7.3 (2026-11-03: validation_context, bash_noop), then 0.8.0 (2026-11-19 on: idle_capped,
   abort_report, failed_output). `idle_capped_ab_2026_11/REPORT.md`: 0.8.0 decodes at ~147 tok/s
   vs ~70 on 0.7.x, so these are new kernels.
6. **What the model does differently after that.** `bot/config.py` is 14,483 bytes, just over the
   13,653-byte read cap at 32K ctx, so the whole-file read is refused in every era. The refusal
   offers `read_file(..., offset=0, limit=500)`, which works: it returns a 13,726-byte window
   covering nearly the whole file (re-run by hand today).
   - October (0.7.0): after the refusal, the model read 3.4 / 4.9 / 4.1 KB windows across the file.
   - 2026-11-03 (0.7.3): mostly took the 500-line call (13,732 bytes) in one read.
   - November (0.8.0): reads `offset=0, limit=60`, then `offset=35, limit=10`, and never returns
     to the file. The `PM_NH_*` reads live at config.py lines 188 to 260.

## Not a new weakness, a frozen coin flip

On 0.7.0 the decode was already non-deterministic at near-ties (`m5max_moe_r2_2026_10/HAND-VERIFY.md`
§ P4e: cold-cache reps diverged at step 3 on identical prompts). The champion's doc-config was REAL in
only 9 of 16 hand-graded runs; the rest were THIN through omitted `PM_NH_*` or invented variables.
One October run (da9cd4bc5326) already took the short-window path and scored 32. 0.8.0 removed the
variance and landed on the incomplete branch.

## Consequences

- Within-session A/Bs on 0.8.0 (idle_capped, abort_report, failed_output, the image-exec slice, the
  idle-wide re-run) compare like with like. **Comparisons against October baselines are confounded
  by the server upgrade**, on this fixture and possibly others.
- The fixture now measures a real, reproducible gap: the champion does not page through a file that is
  just over the read cap. R4 records it on every run.

## Options (not done; decision needed)

1. **Re-baseline the champion on 0.8.0** (10 x 3 shipped defaults, ~35 min) so later A/Bs and the
   October numbers stop being compared directly. Cheapest and removes the confound.
2. **Make the over-cap refusal return the first window** (`LUXE_READ_OVERSIZE_WINDOW=1`, already
   built, opt-in). On SWE-bench n=75 x 3 it was neutral to slightly negative (34.7 vs 35.7). Here it
   would hand over lines 1-~340 at step 1. Worth one maintain_suite arm, not a default change.
3. **Leave it.** The fixture now honestly shows that the model under-reads a 6%-over-cap file. Only
   worth acting on if the same pattern shows up on real work.
