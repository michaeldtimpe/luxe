# splash_engine_ab_2026_09 — facts (m5, read-only collection 2026-09-25)

Root: `~/Downloads/luxe/acceptance/splash_engine_ab_2026_09/suite/` on m5.
All steps below rc=0 per `progress.log` / `addendum/progress.log` — no failed steps found.
**A5 (`bfcl_multi_turn_base_omlx6`) is still running** (started 2026-09-25T04:14:48) — not read/touched.

## Headline comparison

| benchmark | 6-bit oMLX | Splash 4-bit | Δ | wall 6-bit / Splash |
|---|---|---|---|---|
| BFCL simple_python (proxy, A1 vs A2) | 86.50% | 83.75% | −2.75pp | (within A1 13631s / A2 6095s totals, 6 cats) |
| BFCL 6-cat TOTAL (proxy, A1 vs A2) | 75.00% (1440) | 71.94% (1440) | −3.06pp | 13631s / 6095s |
| BFCL multi_turn_long_context (proxy) | 58.50% | 46.00% | **−12.50pp** (worst) | — |
| BFCL multiple (proxy) | 82.00% | 82.00% | +0.00pp | — |
| BFCL parallel (proxy) | 67.00% | 64.50% | −2.50pp | — |
| BFCL parallel_multiple (proxy) | 49.00% | 50.50% | +1.50pp | — |
| BFCL irrelevance (proxy) | 92.08% | 89.58% | −2.50pp | — |
| BFCL simple_python (P1 direct, no proxy) | 85.25% | n/a (Splash 400s on dotted tool names, SKIP) | — | 2361s |
| BFCL multi_turn_base (raw) | — (A5 running, not yet available) | 51.50% | vs stored baseline 63.5% (M5, omlx6) → **−12.0pp implied** | P3 1614s |
| GSM8K acc | 97.60% (parse 100.00%) | 97.20% (parse 99.60%, 1 think_only) | −0.40pp | P1 4940s (19.8s/item) / P3 1422s (5.7s/item) |
| CodeNeedle http_server.py pass | 100.00% (11 fns) | 81.82% (A3, reasoning=none) | −18.18pp | — |
| CodeNeedle jquery.js pass | 87.50% (16 fns) | 75.00% (A3) | −12.50pp | P1 724s / A3 93s |
| SWE-bench n=14 patch-produced | 11/14 | 13/14 | +2 | 1256s (89.7s/inst) / 603s (43.1s/inst) |
| SWE-bench mechanical PASS | 10/14 | 13/14 | +3 | — |
| SWE-bench strong-or-plausible | 8/14 (strong=8, plausible=0) | 11/14 (strong=6, plausible=5) | +3 | — |

Splash is 2.2–2.5× faster wall across BFCL/GSM8K/SWE-bench/CodeNeedle, but scores lower on BFCL total (−3.06pp, driven by multi_turn_long_context −12.5pp) and CodeNeedle (−12 to −18pp). SWE-bench mechanical/strong-or-plausible favors Splash, but hand-read (below) says the classifier's "strong" label isn't a reliable quality gap — Splash's patches in 2/3 disagreement cases examined look at least as correct as oMLX6's, just penalized by the coverage/jaccard heuristics for being terser.

## 1. Per-step rc/wall/headline (source: `progress.log` + `<step>.log` tails)

| step | rc | wall_s | headline |
|---|---|---|---|
| P1/bfcl_simple_python | 0 | 2361 | 341/400 (85.25%) |
| P1/gsm8k | 0 | 4940 | acc=97.60%, parse=100.00%, failures={'none':250} |
| P1/codeneedle | 0 | 724 | http_server.py 100.00% (11fn); jquery.js 87.50% (16fn) |
| P1/swebench_omlx6 | 0 | 1256 | 11/14 non-empty patch |
| P1/swebench_omlx6_classify | 0 | 0 | strong=8 plausible=0 wrong_location=1 wrong_target=1 (empty=3, new_file=1 unclassed); mech PASS 10/14, strong-or-plausible 8/14 |
| P2/mmlu_6bit | 0 | 2600 | micro=67.10% macro=65.87% (n=10158) |
| P2/arc_6bit | 0 | 129 | 79.61% (n=1172) |
| P2/mmlu_4bit | 0 | 2348 | micro=70.43% macro=69.64% (n=10158) |
| P2/arc_4bit | 0 | 99 | **58.62%** (n=1172) — see anomaly below |
| P3/bfcl_6cat | SKIP | — | deferred: Splash 400s on dotted BFCL tool names (654/1240) |
| P3/bfcl_multi_turn_base | 0 | 1614 | 103/200 (51.50%) |
| P3/gsm8k | 0 | 1422 | acc=97.20%, parse=99.60%, failures={'none':249,'think_only':1} |
| P3/codeneedle | SKIP | — | deferred: Splash ignores /no_think, reasoning eats 2048 budget |
| P3/swebench_splash | 0 | 603 | 13/14 non-empty patch |
| P3/swebench_splash_classify | 0 | 1 | strong=6 plausible=5 wrong_target=2; mech PASS 13/14, strong-or-plausible 11/14 |
| P4/restore_smoke | 0 | 15 | READY — chat + code drill green |
| A1/bfcl_6cat (proxy, omlx6) | 0 | 13631 | TOTAL 75.00% (1440), err=0 both arms |
| A2/bfcl_gate (proxy, splash, n=18 smoke) | 0 | 72 | 16/18 (88.89%); gate: requests=52 with_renames=11 http_4xx5xx=0 record_errors=0 |
| A2/bfcl_6cat (proxy, splash) | 0 | 6095 | TOTAL 71.94% (1440) |
| A3/codeneedle (Splash, --default-reasoning-effort none) | 0 | 93 | http_server.py 81.82%; jquery.js 75.00%; empty=0/27 (both early n=6 and final n=27 checks) |
| A4/restore_smoke | 0 | 15 | READY — chat + code drill green |
| A5/bfcl_multi_turn_base_omlx6 | **IN PROGRESS** | — | started 04:14:48, not collected |

Proxy error scan (`grep -Ei "error|4xx|5xx" addendum/{A1,A2}/proxy.log`): **0 hits both arms** — no HTTP 4xx/5xx or proxy errors through the tool-name proxy.

## 2. GSM8K / CodeNeedle detail

GSM8K: 6-bit acc 97.60%, parse 100.00%, avg 19.8s/item (250 items, 4940s). Splash acc 97.20%, parse 99.60% (1 `think_only` failure — model emitted only `<think>`, no final answer), avg 5.7s/item (1422s) — 3.5× faster.

CodeNeedle per-file (source: `P1/codeneedle.log`, `addendum/A3/codeneedle.log`):
| file | 6-bit pass | Splash pass (A3) |
|---|---|---|
| http_server.py (11 fns) | 100.00% | 81.82% |
| jquery.js (16 fns) | 87.50% | 75.00% |
wall: P1 724s vs A3 93s (7.8× faster). A3 ran with `--default-reasoning-effort none` per the deferred-then-resolved Splash /no_think issue.

## 3. SWE-bench n=14 per-instance (source: `P1/swebench_omlx6_classify.log`, `P3/swebench_splash_classify.log`)

| instance | omlx6 class | splash class |
|---|---|---|
| sphinx-doc__sphinx-10435 | strong | strong |
| sympy__sympy-13031 | strong | plausible (hunks 4 vs gold 2) |
| matplotlib__matplotlib-20676 | empty_patch | plausible |
| matplotlib__matplotlib-20826 | wrong_location | plausible |
| sphinx-doc__sphinx-10673 | wrong_target | wrong_target |
| astropy__astropy-12907 | strong | strong |
| django__django-10914 | strong | strong |
| django__django-10973 | strong | strong |
| scikit-learn__scikit-learn-10844 | strong | strong |
| pytest-dev__pytest-10081 | strong | plausible |
| matplotlib__matplotlib-13989 | strong | strong |
| mwaskom__seaborn-3069 | empty_patch | empty_patch |
| sympy__sympy-12419 | new_file_in_diff (stray test file) | plausible |
| pylint-dev__pylint-4604 | empty_patch | wrong_target |

Hand-read of the 3 biggest disagreements (patches from `P1/swebench_omlx6/predictions.json`, `P3/swebench_splash/predictions.json`):
- **pylint-4604**: omlx6 empty. Splash added an `astroid.Attribute` branch to append `expr.name` — on-target file, just misses gold's second edit in `pylint/constants.py` (hence `wrong_target`). Splash's attempt is a real, sane partial fix; omlx6 didn't try.
- **sympy-12419**: both touch `matexpr.py::_entry` with the same idea (KroneckerDelta for off-diagonal). Splash's `return KroneckerDelta(i, j)` unconditionally is simpler and functionally equivalent to omlx6's verbose i==j/is_number branching. omlx6 also smuggled a stray new file `repo_root_test.py` into the diff (why it's `new_file_in_diff` not `strong`) — a real defect. Splash's core edit is at least as good and cleaner.
- **matplotlib-20826**: gold loc `axis.py:806`. Splash removes the two `_reset_major/minor_tick_kw()` calls right there (matches the actual fix) but only gets `plausible` on a size mismatch (2 lines vs gold's 9-line hunk). omlx6 edited the wrong location (774) with an elaborate but misplaced preserve-state hack — `wrong_location` looks correct, i.e. classifier isn't crediting junk, if anything it's harsh on Splash.

Overall: classifier isn't crediting junk (no gold/empty-patch mismatches found), but strong-vs-plausible undercredits Splash's terser, still-correct patches — raw score gap likely overstates any real SWE-bench quality gap.

## 4. Cited stored baselines (not recollected)
BFCL raw 2026-05-27 per-cat: simple_python 84.25, multiple 81.50, parallel 65.50, parallel_multiple 48.00, irrelevance 92.08, multi_turn_long_context 39.00. multi_turn_base 63.5% (M5, omlx6). MMLU 68.84% (different subset, stratification bug — not comparable). P1 bfcl_simple_python today: 85.25% (direct, non-proxy).

## 5. Maintain/microbench (already known; tables below, source `../microbench_{omlx6,splash4}.txt`)
maintain reps walls: omlx6 11.1/7.3/7.2 min, avg 64.4/41.6/41.3s, hand-verify 29R/1T. splash4 6.3/5.8/4.4 min, 36.3/33.3/24.5s, 29R/1T.

microbench_omlx6.txt:
```
    ctx cold_ttft_s prefill_tps  cached_ttft_med_s decode_tps_med compl_toks_med
   2000       0.741      2784.6              0.304          117.2            202
   8000       2.212      3735.4              0.373          113.4            175
  16000       4.516      3659.1              0.422          110.2            156
  32000      10.641      3138.7              0.663          100.6            191
```
microbench_splash4.txt:
```
    ctx cold_ttft_s prefill_tps  cached_ttft_med_s decode_tps_med compl_toks_med
   2000       0.376      5491.9              0.089          186.4            201
   8000       1.413      5846.1              0.129          178.0            174
  16000       3.031      5452.0              0.105          150.5            173
  32000       7.279      4589.1              0.131          145.8            174
```
Splash: ~1.5–2× prefill TPS, ~1.4–1.6× decode TPS, much lower TTFT at every ctx size vs 6-bit oMLX.

## Anomalies / flags
1. **P2/arc_4bit = 58.62% vs arc_6bit 79.61%** (−21pp) while mmlu_4bit (70.43%) actually *beats* mmlu_6bit (67.10%, +3.3pp) on the same weights/engine swap. This is inconsistent — a 21pp ARC regression alongside an MMLU improvement on the same in-process mlx 4-bit load smells like a bug (prompt template, `by_choices` n=4/1165 acc=0.58 driving it — not a small-n artifact) rather than a real capability drop. Worth a rerun/inspection before citing arc_4bit.
2. **P3/bfcl_6cat and P3/codeneedle were SKIPPED**, not run — both deferred for known Splash issues (dotted tool names → 400s; ignores `/no_think`). The addendum (A1–A3) exists specifically to work around these via the tool-name proxy and `--default-reasoning-effort none`, so P3's gap is filled by A1–A3, not by P3 itself.
3. **multi_turn_long_context is Splash's weakest category by far** (−12.5pp vs omlx6, both via proxy) — the single biggest BFCL regression.
4. A5 (fresh omlx6 multi_turn_base rerun) was **in progress** at collection time — the 63.5% baseline cited above predates this suite; once A5 finishes it will be the apples-to-apples comparison to P3's 51.50%.
5. No rc≠0, no proxy 4xx/5xx, no truncation markers found anywhere in scanned logs.
