# Splash vs oMLX engine A/B + luxe vs opencode harness — splash_engine_ab_2026_09

**Model:** Qwen3.6-35B-A3B, 4-bit. **Host:** m5 (M5 Max, 128 GB). **Date:** 2026-09-24.
**Engines:** oMLX 0.6.4 (`Qwen3.6-35B-A3B-4bit`, mlx-community, rsynced from m1) vs
Splash 1.0.2 / brew `incoai/tap/splash` (`incoai/Qwen3.6-35B-A3B-Splash`, Inco's own
4-bit package + mandatory DFlash 2 speculative draft, int8 KV default, reasoning on),
one resident on :8000 at a time (oMLX stopped via `brew services` for the Splash arm,
restored after — `luxe smoke --chat --code` on manifest defaults READY 16s rc=0).
**CONFOUND:** not byte-identical weights — Inco's 4-bit quant ≠ mlx-community's 4-bit;
quality deltas below may be quant, not engine.
**Harnesses:** luxe maintain_suite (10 fixtures × 3 reps/engine) vs a new
`scripts/opencode_harness.py` (uncommitted; opencode 1.18.30 `run --auto`, isolated
HOME/XDG/OPENCODE_CONFIG, temp 0, ctx 65536/out 8192, same fixture goal text + one
neutral line, graded by luxe's `grade_fixture`; 10 fixtures × 2 reps/engine). Printed
max is 4/5 on both harnesses here (`gh pr create` fails offline) — compare on outcome
points + hand-verify, not the printed grade.
**Method:** every number below is read from result.json/summary.json or hand-verified
by reading diffs against fixture goal text — not either grader. Verified from m5,
`~/Downloads/luxe/acceptance/splash_engine_ab_2026_09/`, read-only except this file.

---

## Wire probe (Splash)

`/v1/models` advertises only its own id; wrong model id → 404; auth header ignored;
unknown top-level fields (`num_ctx`, `repeat_penalty`, `repetition_penalty`) accepted
and silently no-op'd; OpenAI `tool_calls` clean; SSE clean, no keepalive lines; temp 0
deterministic with `reasoning_effort: none`. No luxe wire changes needed to talk to it.

## Smoke (`luxe smoke --chat --code --skip-fallback --no-fix`)

| Engine | Total | chat | code |
|---|---|---|---|
| oMLX | 15s | 8s | 7s |
| Splash | 6s | 2s | 4s |

## Microbench (`engine_microbench.py`, streaming, temp 0, reasoning off, 256 max tok, 3 cached reps)

| ctx | cold TTFT oMLX→Splash | prefill tok/s | cached TTFT median | decode tok/s median |
|---|---|---|---|---|
| 2K | 0.758→0.376s | 2719→5492 | 0.273→0.089s | 152.5→186.4 |
| 8K | 2.075→1.413s | 3982→5846 | 0.338→0.129s | 138.2→178.0 |
| 16K | 4.313→3.031s | 3832→5452 | 0.413→0.105s | 131.8→150.5 |
| 32K | 10.665→7.279s | 3132→4589 | 0.671→0.131s | 118.9→145.8 |

Measured ratios: decode 1.13–1.29×, cached TTFT 3.1–5.1×, prefill 1.4–2.0×. Inco's
own M5-Pro-16-core numbers claim decode 1.7×, cached TTFT 6.6×, prefill 1.3× at 32K —
M5 Max's larger GPU narrows the decode/TTFT win and widens the prefill one relative to
their marketing numbers; **the cached-prefix win, not decode, is what's driving the
end-to-end result below.**

## luxe maintain_suite — printed

All 6 reps 10/10 (40/50, `pr_opened=0` everywhere — no GitHub remote offline).

| Engine | Rep wall (min) | Avg wall/fixture (s) | Avg tokens/fixture |
|---|---|---|---|
| oMLX | 11.9 / 11.1 / 11.1 | 69.4 / 65.2 / 65.1 | 167k / 165k / 154k |
| Splash | 6.3 / 5.8 / 4.4 | 36.3 / 33.3 / 24.5 | 135k / 128k / 105k |

→ **~2.0–2.6× faster wall, ~20–30% fewer tokens**, all 6 reps 10/10.

## opencode harness — printed

All 4 runs 40/50, outcome 30/30, 0 timeouts/errors (20 cells/run, `outcome_points`
3/3 on every one).

| Engine | Rep wall (min) | Avg s/fixture | Avg tool calls |
|---|---|---|---|
| oMLX | 9.2 / 6.9 | 55.4 / 41.3 | 13.2 / 10.4 |
| Splash | 4.6 / 3.5 | 27.3 / 20.8 | 8.6 / 8.6 |

Token counts not comparable across harnesses (opencode counts cache reads).
Harness asymmetry: opencode has `webfetch` (live internet, used on deps-audit); luxe's
bench tool surface has no web tools, only the manage-gated `cve_lookup`.

---

## Hand-verify (the real result) — 100 cells total

| Cell | REAL | THIN | VACUOUS | DAMAGING | n |
|---|---|---|---|---|---|
| luxe × oMLX | 24 | 6 | 0 | 0 | 30 |
| luxe × Splash | 29 | 1 | 0 | 0 | 30 |
| opencode × oMLX | 14 | 5 | 0 | 1 | 20 |
| opencode × Splash | 16 | 4 | 0 | 0 | 20 |

**None of these show in the printed score** — 30/30 outcome or 4/5 fixture-score
everywhere, on both harnesses, both engines.

**luxe × oMLX (24/6/0/0):** `neon-rain-implement-reset-shortcut` rep3 THIN —
`e.shiftKey && e.key === 'r'` (lowercase) never fires under a real Shift+R press (DOM
gives uppercase `'R'`); existing test suite doesn't exercise the handler so
`tests_pass` misses it. `nothing-ever-happens-document-config` THIN on reps 1 and 3 —
38 and 26 env vars documented vs 57 in rep2, plus wrong `bot/config.py:48/51` line
cites (actual 117/118) in rep3.

**luxe × Splash (29/1/0/0):** only THIN is the same fixture family —
`neon-rain-implement-reset-shortcut` rep3 works (correct `'R'`, tests pass) but
bypasses the required `src/input/` system, adding a raw
`document.addEventListener('keydown', …)` inline in `Game.js`'s constructor with no
dispose path — an architecture miss invisible to `tests_pass`. `doc-config` hits
58–60 vars all 3 reps (best of any of the four cells); all 3 neon-rain reps use the
correct `'R'`.

**opencode × oMLX (14/5/0/1):** the one DAMAGING — `lpe-rope-calc-implement-strict-flag`
rep2 silently replaces the `scan_lmstudio()` call with `scan_hf_cache()`, dropping
MLX/LM Studio detection in default (non-strict) mode; confirmed in the rep's
diff.patch, unrelated to the requested `--strict` feature. Also: rep1 neon-rain
lowercase-`'r'` bug (independently reproduced, third occurrence across the matrix);
`doc-config` fabricates `DASHBOARD_PORT` default `8080` in both reps (source has no
default — falls back to `None`/dashboard skipped).

**opencode × Splash (16/4/0/0):** neon-rain lowercase-`'r'` bug in both reps (fourth
occurrence — same bug independently reproduced by 3 of 4 cells, omlx-luxe rep2 is the
only variant to get it right). `nothing-ever-happens-manage-deps-audit`: SECURITY-AUDIT.md
cites 4 CVEs with CVSS scores, but every `webfetch` call in its own trace returned only
nav chrome/search shells — confabulated sourcing, confident and wrong, not a capability
gap (it tried, never got past the shell, reported anyway).

**Cross-cutting:** opencode never ran tests/linters itself in any of the 4 runs (read/
edit/occasional `bash ls` only — verification is entirely the harness grader). All 4
opencode strict-flag reps and every luxe `document-typing` rep commit a stray
`__pycache__/*.pyc` — repo pollution neither harness guards against.
**Determinism:** luxe/oMLX byte-identical diffs on 6/10 fixtures; opencode/Splash
rep1=rep2 byte-identical on 9/10 (near-zero input tokens = cache hit); opencode/oMLX
varies rep-to-rep on most fixtures despite temp 0.

## Incidental luxe bug (both arms, not engine-related)

`nothing-ever-happens-manage-deps-audit`: the model self-commits via allowlisted git
before `pr.py:_do_commit`'s dirty check runs, which then sees a clean tree and reports
`failed_no_mutations_produced`. Branch `…-pinned-19` was never created in any of the 6
maintain_suite runs (confirmed absent on origin in every rep). Not a scoring defect —
grader's `git diff base_sha..HEAD` still finds the real diff — but a bookkeeping bug;
recovered via `~/.luxe/runs/<id>/synthesizer.md`, not the branch ref.

---

## Verdict

1. **Engine.** Splash is a real, large end-to-end win for luxe on this host
   (~2–2.6× wall/rep, 20–30% fewer tokens), driven more by cached-prefix TTFT (3–5×)
   than decode (1.1–1.3×). No correctness cost observed — luxe×Splash is the
   best-hand-verified of all four cells (29/30 REAL). The quant confound means
   "Splash didn't hurt" is solid; "Splash helped quality" is not — different 4-bit
   packages, not a controlled ablation.
2. **Harness.** opencode is faster per fixture on both engines but lower verified
   quality (14–16/20 REAL, 70–80%, vs luxe's 24–29/30, 80–97%), including one
   DAMAGING diff and one confabulated-sourcing case; luxe produced zero
   DAMAGING/VACUOUS across all 60 of its cells.
3. **Limits.** n=3/2 reps, 10 fixtures, one host. Single-champion policy still applies:
   Splash serves only 2 models, 4-bit only (bench champion is 6-bit), requires M3+
   (excludes m1, neo; m4 eligible), has no oMLX admin API (`luxe pull`/`unload`/
   `repair`/cold-cache don't apply), and defaults to temp=0 reasoning-on — a config
   difference from the champion's own defaults.
4. **Options — not decided here.**
   (a) add Splash as an opt-in `backends:` entry on m5/m4 for `luxe chat`/`luxe code`
   (`engine: llama-server` to suppress oMLX-specific diagnostics) — fits the existing
   chat-only carve-out, not a bench-path change;
   (b) a 6-bit-champion-vs-Splash-4bit bench, same weights family where possible,
   before touching the bench path;
   (c) fix the deps-audit PR-bookkeeping bug (self-commit races `_do_commit`);
   (d) commit `scripts/opencode_harness.py` (currently untracked).
   (e) **2026-09-25 addendum below executes (b):** 6-bit-champion-vs-Splash-4bit BFCL/SWE-bench/CodeNeedle/GSM8K/microbench/maintain_suite comparison — see "Addendum (2026-09-25)" at the end of this file.

## Files (`~/Downloads/luxe/acceptance/splash_engine_ab_2026_09/`)

- `engine_microbench.py`, `microbench_{omlx4,splash4}.{json,txt}` — microbench + raw output
- `run_arm.sh`, `swap_to_splash.sh`, `finish.sh` — arm orchestration
- `variants_omlx4.yaml`, `variants_splash4.yaml` — maintain_suite variant configs
- `omlx4/`, `splash4/` — per-engine maintain_suite run dirs (result.json, runs/, pr_state.json)
- `splash-8000.log`, `omlx4.nohup.log`, `swap.log`, `swap.nohup.log`, `finish.log`,
  `finish.nohup.log`, `restore_smoke.log` — engine/orchestration logs
- `hand_verify/HV-omlx-luxe.md`, `HV-splash-impl.md`, `HV-splash-doc.md`,
  `HV-opencode.md` — full per-cell hand-verify tables cited above

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>

## Addendum (2026-09-25) — 6-bit champion vs Splash 4-bit

Executes Verdict item 4(b) above: a same-family-where-possible bench of the pinned
`Qwen3.6-35B-A3B-6bit` champion against Splash 4-bit, on BFCL (6 categories +
multi_turn_base), GSM8K, CodeNeedle, SWE-bench n=14, ARC/MMLU (in-process quant
ablation), microbench, and maintain_suite. Full per-step rc/wall/headline tables,
cited baselines, and anomaly notes: `suite/FACTS.md` (source for every number
below). Root: `suite/` on m5; workaround code + logs in `suite/addendum/`.

### Headline

| benchmark | 6-bit oMLX | Splash 4-bit | Δ | wall (6-bit / Splash) |
|---|---|---|---|---|
| BFCL 6-cat TOTAL (proxy) | 75.00% | 71.94% | −3.06pp | 13631s / 6095s |
| BFCL multi_turn_base (fresh, both engines) | 63.00% | 51.50% | **−11.50pp** | 3723s / 1614s |
| BFCL multi_turn_long_context (proxy) | 58.50% | 46.00% | **−12.50pp** | — |
| BFCL simple_python (proxy) | 86.50% | 83.75% | −2.75pp | — |
| BFCL simple_python (direct, no proxy) | 85.25% | n/a (400s on tool names) | — | reproduces stored 84.25% |
| BFCL irrelevance / multiple / parallel / parallel_multiple | 92.08/82.00/67.00/49.00% | 89.58/82.00/64.50/50.50% | −2.5/0/−2.5/+1.5pp | — |
| GSM8K acc | 97.60% | 97.20% | −0.40pp | 4940s / 1422s |
| CodeNeedle http_server.py / jquery.js | 100.00% / 87.50% | 81.82% / 75.00% | −18.2 / −12.5pp | 724s / 93s |
| SWE-bench n=14 strong-or-plausible | 8/14 | 11/14 | classifier artifact, see below | 1256s / 603s |
| maintain_suite hand-verify (3 reps) | 29 REAL / 1 THIN | 29 REAL / 1 THIN | tie | 64.4–41.3 s/fix / 36.3–24.5 s/fix |

Splash is 2–8× faster wall depending on task; no measurable quality loss on
short/single-turn work (maintain identical, GSM8K −0.4pp, single-turn BFCL within
~3pp) but a real, consistent loss once context or turn count grows (multi-turn
categories −11.5 to −12.5pp, CodeNeedle −12 to −18pp).

### BFCL detail

A1/A2 ran all 6 categories through both engines via a tool-name proxy (Splash
1.0.2 HTTP-400s on tool names outside `[A-Za-z0-9_-]`; `suite/addendum/toolname_proxy.py`).
0 proxy errors (grep for error/4xx/5xx on both proxy.log = 0 hits); Splash gate
smoke 52 req / 11 renamed / 0 errors. Both arms hit the shared 1024-max_tokens
harness cap heavily (449 vs 455 of 1440 calls) — same on both sides, a harness
limit, not an engine difference.

A5 reran `multi_turn_base` fresh on 6-bit (126/200, 63.00%, 3723s) — reproduces
the stored 63.5% baseline (M5, omlx6) closely enough to trust the −11.5pp gap
against Splash's P3 result (51.50%, 1614s) as real, not baseline drift.

P1 reran `simple_python` direct (no proxy) at 85.25% vs the stored 84.25% raw
baseline — baseline reproduces.

### CodeNeedle — different failure modes, not just a score gap

6-bit oMLX honours prompt-level `/no_think` cleanly. Splash ignores it; A3 used
server-side `--default-reasoning-effort none` instead. Hallucination counts
differ sharply under this config: 6-bit 1434/1960 vs Splash 41/86 — Splash
reasons far less verbosely but still passes fewer functions (81.8%/75.0% vs
100%/87.5%). Pass rate, not hallucination count, is the quality signal here.

### SWE-bench n=14 — call it a tie, not a Splash win

Raw classifier: 6-bit strong-or-plausible 8/14, Splash 11/14. Hand-read of the
disagreements (no Docker FAIL_TO_PASS scoring available on m5) finds the
classifier mis-ranking both directions: 6-bit's "strong" `sympy-12419` smuggles
a stray debug file into the diff; Splash's `matplotlib-20826` lands on gold's
exact line but scores only "plausible" on hunk-size mismatch. Net: a tie on
inspected quality, not the +3 the raw numbers imply.

### Quant ablation (P2, in-process mlx_lm — not Splash's quantization)

Same weights family, tokenizer, items, and order, only bits 6→4, run in-process
(not through Splash): ARC-Challenge 79.61% → 58.62%, a real drop with a strong
'A'-position bias emerging at 4-bit (56.7% vs 36.1%). MMLU 67.10% vs 70.43% is
**not comparable** — `benchmarks/mmlu/run.py:62-72` stratifies `--limit`
per-subject (`limit // 57`) and never redistributes, so `--limit 14042` only
selects 10158 of them; unfixed. This ablation shows 4-bit quantization alone
can cost real accuracy on some evals, but doesn't isolate whether it explains
the BFCL/CodeNeedle multi-turn gap above (see verdict below).

### Incidental findings

1. Splash 1.0.2 HTTP-400s on tool names outside `[A-Za-z0-9_-]` — worked around
   via `suite/addendum/toolname_proxy.py`, both engines.
2. Splash ignores prompt-level `/no_think`; server-side
   `--default-reasoning-effort none` works instead.
3. `benchmarks/mmlu/run.py` per-subject `--limit` stratification bug (above) —
   unfixed, blocks any `--limit` above the natural per-subject floor.
4. deps-audit PR-bookkeeping bug (self-commit races `_do_commit`) — already
   noted in the main report above; still open.

### Addendum verdict

No measurable loss from Splash 4-bit on short agentic/coding work; a real and
consistent loss as context grows or turns accumulate, at 2–8× less wall. The
cause is **not isolated** — 4-bit weights and Splash's int8 KV-cache default
are confounded in every comparison above; a `--kv-format bf16` Splash run
would separate them and is the natural next step if this gets re-opened. Per
`luxe.sdd` single-champion policy, the decision stands: luxe stays on
`Qwen3.6-35B-A3B-6bit` via oMLX for the bench/maintain path. This addendum
records what staying costs in wall time and what it buys in long-context
accuracy — it does not reopen the pin.

### Addendum files (under `~/Downloads/luxe/acceptance/splash_engine_ab_2026_09/`)

- `suite/run_suite.sh`, `suite/progress.log`, `suite/P1/`–`suite/P4/` — main
  arm orchestration + per-step logs (BFCL, GSM8K, CodeNeedle, SWE-bench,
  ARC/MMLU quant ablation, smoke restore)
- `suite/addendum/toolname_proxy.py` + tests, `run_addendum.sh`, `run_a5.sh`,
  `bfcl_compare.py`, `A1/`–`A5/` — tool-name proxy workaround + fresh
  multi_turn_base rerun
- `suite/FACTS.md` — full per-step rc/wall/headline tables, cited baselines,
  anomaly notes (source for every number above)
- `hand_verify/HV-omlx6-luxe.md` — 6-bit maintain_suite hand-verify (29 REAL/1 THIN)
- `suite/microbench_omlx6.txt`, `suite/microbench_splash4.txt` — cached-TTFT/
  prefill/decode tables at 2K/8K/16K/32K ctx
