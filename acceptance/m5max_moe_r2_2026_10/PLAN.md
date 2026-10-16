# m5max_moe_r2 — six-arm bake-off plan (weekend of 2026-10-16)

**Why this exists:** explicit user request (2026-10-16) for a re-bench, which
clears the single-champion carve-out. The champion pin
(`Qwen3.6-35B-A3B-6bit`, `configs/single_64gb.yaml`) is **untouched** until
a REPORT.md says otherwise *and* the user approves a promotion. Structure follows
RESUME.md § "M5 Max MoE bake-off".

**Host:** m5 (M5 Max 128 GB, oMLX 0.7.0, `max_model_memory: 96GB`, 862 GB
free, `sleep 0`). Built and planned from m1; every run happens on m5.

## Arms

All 6-bit affine, group 64 (champion parity), temperature 0 (luxe invariant).

| # | label | weights | class | engine | provisioning |
|---|---|---|---|---|---|
| 1 | `qwen3.6-35b-a3b-6bit-ref` | on m5 | MoE 35B-A3B, VLM | vlm | none (champion) |
| 2 | `qwen3.6-27b-6bit-ref` | on m5 | dense 27B, VLM | vlm | none (fallback) |
| 3 | `muse-glimmer-30b-6bit` | `mlx-community/Muse-Glimmer-30B-6bit` (26.4 GB) | dense 28B, VLM | vlm | `luxe pull --hf` |
| 4 | `k2-horizon-mova-36b-a4b-6bit` | **self-converted** from `IFM/K2-Horizon-MoVA-36B-A4B` | MoE 37B-A4B, text | llm? | download bf16 + convert (below) |
| 5 | `laguna-xs-2.1-6bit` | `mlx-community/Laguna-XS-2.1-6bit` | MoE 33B-A3B, text | llm | `luxe pull --hf` |
| 6 | `lfm2.5-24b-a2b-6bit` | `LiquidAI/LFM2.5-24B-A2B-MLX-6bit` (19.5 GB) | MoE 24B-A2B, text | llm | `luxe pull --hf` |

Arms 1–2 are references on today's substrate, not previous results: the
substrate moved since August (cache-friendly compaction default-ON 2026-10-15,
server-truth ctx, read budget).

Left out on purpose: Qwen3.8-Flash-Next (180B total — 6-bit ≈135 GB doesn't
fit), Qwen3.8-27B (refuted 2026-08-18; re-open trigger = official 6-bit,
not met), Muse-Spark-816B, MiniCPM-V-4.7 REAP (pruned VLM — uninterpretable).

## What makes this round different

Arms 4–6 are **text-only**. The champion pays the VLM-engine penalty on every
turn (it is multimodal); these don't. A challenger that matches on
hand-verified quality could win wall-clock by a real margin — the first time
that is plausible. So wall and prefill tok/s are first-class results.

The Gemma-4 lesson applies to every arm: **a printed pass is not a result.**
Speed bought by skipping verification (zero lint/typecheck/git_diff) is a
refutation, not a win.

## Phases

### P0 — preflight tool (m1, Fri) — BUILD FIRST
`benchmarks/maintain_suite/preflight.py`: reads a variants YAML, and per arm
refuses to let a run burn on a plumbing failure. RESUME.md:727 proposed it;
it would have caught all four of Glimmer's August blockers in seconds.

Static (no model load, seconds):
- weights in the oMLX store, real bytes, every shard the index names present,
  no dangling symlink (`modelstore`)
- `config.json` `model_type` resolvable by the INSTALLED oMLX
  (mlx-lm, mlx-vlm, or an `omlx/patches/<type>` registration)
- predicted engine (llm/vlm, oMLX's own `detect_model_type`)
- quantization: bits/group/mode vs the champion → WARN on mismatch
- chat template carries a tool block (`chat/modelcaps.from_template`)
- oMLX's tool-parser inference on that template → a parser name, or FAIL
  (the silent text-fallback-drop class)

Live (one model at a time, minutes):
- `/v1/models` lists the id
- ping (cold-load time recorded; reasoning-channel budget handled)
- structured tool call → `tool_calls` populated, `finish_reason=tool_calls`;
  markup in `content` = FAIL "unparsed dialect"
- optional `--drill`: `luxe smoke --code` drill against the arm's model

Output: `preflight.json` + `PREFLIGHT.md` in the run dir; exit 1 on any FAIL.
Tests in `tests/test_bench_preflight.py`, no oMLX dependency.

### P1 — provisioning (m5, Fri evening, ~1–2 h, mostly download)
1. `luxe pull --hf` arms 3, 5, 6 (through oMLX's downloader — never a second
   `snapshot_download`).
2. **K2 conversion** (arm 4):
   - download `IFM/K2-Horizon-MoVA-36B-A4B` bf16 (~75 GB) via oMLX admin API
   - convert inside **oMLX's own Python env** with
     `omlx.patches.k2_horizon.apply_k2_horizon_patch()` applied first (it
     registers `mlx_lm.models.k2_horizon`; stock mlx-lm lacks it):
     `mlx_lm.convert --hf-path <bf16> -q --q-bits 6 --q-group-size 64
     --mlx-path <stage>/K2-Horizon-MoVA-36B-A4B-6bit`
   - **verify parity:** `quantization` block = `{bits:6, group_size:64,
     mode:affine}` with router gates at 8-bit like the champion's; if gates
     came out 6-bit, re-run with the matching quant predicate
   - verify the `chat_template.jinja` + tokenizer files came across byte-equal
     to upstream (conversion has dropped templates before)
   - import via `luxe pull --from <stage>`; `luxe pull --list` shows it real
   - expected ~30 GB; keep the bf16 until R1 is read, then delete (it is
     the re-quant source if parity is off)
   - compare against `DreamFoundries/…-MLX-6bit` only as a sanity check
     (config diff), never as the benched weights
3. Record the registered model ids from `/v1/models`; correct the variants
   file if oMLX's id differs.

### P2 — preflight all six (m5, ~20 min)
`preflight.py --variants variants_m5max_moe_r2.yaml --live --drill`.
A FAIL drops only that arm and gets fixed or written up; it never blocks the
others. Specific things to watch:
- Glimmer: oMLX's native `atem` parser must claim it. Our
  `vendor/omlx_patches/muse_glimmer` is NOT installed and should stay that way
  (upstream-first) unless native parsing fails.
- K2: confirm the runtime-registered `k2_horizon` tool parser fires; record
  the default `reasoning_effort` and whether the ANE-prefill patch engages.
- Laguna / K2 / LFM: record reasoning on/off defaults and reasoning tokens
  per turn. Policy: **each arm runs its shipped defaults at temp 0**; vendor
  sampling (Laguna benched at temp 1.0) is a caveat, not a per-arm tweak.

### P3 — R1 elimination (m5, Fri night → Sat morning, ~6–8 h)
1 rep × 10 fixtures × 6 arms, `--per-fixture-timeout 1800 --all`.
Precedent: 4 arms × 10 ≈ 4h10m. Arm order puts the references first and the
arm we can most afford to lose last. Save the run-id manifest
(`scripts/save_run_id_manifest.py`) as soon as it finishes.

### P4 — hand-verify R1 (Sat)
Every printed pass of arms 3–6 read from the fixture branch diff against the
goal text → REAL / THIN / VACUOUS, plus the tool-mix (lint/typecheck/git_diff
counts) vs the references. `HAND-VERIFY.md`.
**Advance to R2 only if:** ≥9 REAL of 10, no fixture left worse than
`base_sha`, verification calls present.

### P5 — R2 confirmation (Sat night → Sun, ~12–20 h)
Survivors + both references × 3 reps × 10. If more than two challengers
survive, R2 the two best by hand-verified REAL count, then wall.
Contested cells (anything not 3/3) get hand-read again.

### P6 — long smoke tests (Sun, in R2 gaps or after — never concurrent)
Only for R2 survivors; the bench and these never share the server
(single residency; it would contaminate timing).
- `luxe smoke --chat --code --model <arm>` × 10 back-to-back → pass rate,
  p50/p95 turn time (flake hunting — a fallback that works 9/10 isn't one)
- long chat session: a scripted 25–30-turn piped REPL over a real repo with
  turn carry ON → prefix-cache hit rate, compaction phases reached,
  turn-time drift
- `scripts/bigread_drill.py` → the big-read path that hung on 2026-08-24
- `scripts/capacity_drills.py` structure (9 drills) if time remains

### P7 — REPORT.md + decision (Sun night / Mon)
Two separate questions, two verdicts:
1. **Champion:** a MoE challenger (K2 / Laguna / LFM) replaces Qwen3.6-35B-A3B
   only with ≥ parity on hand-verified 3-rep quality AND a material wall
   win. Ties keep the champion.
2. **m5 dense fallback slot:** Glimmer vs Qwen3.6-27B, same bar.
Nothing is promoted by this plan — a promotion is a separate, user-approved
change to `single_64gb.yaml` / `chat.yaml` `hosts.m5`. m1/m4 (60 GB) manifest
implications are a follow-up (they run 4-bit; not benched here).
Losers' weights get deleted from m5 after the report; lessons.md + memory
updated either way.

## Risks
- **K2 conversion parity** — the one self-made artifact. The parity check in P1
  is mandatory; a mismatched quant makes arm 4 uninterpretable.
- **K2 on the llm engine** — `k2_horizon` exists only as an oMLX runtime
  patch + an mlx-vlm dir; if oMLX misroutes it the preflight catches it.
- **Reasoning verbosity** — Qwen3.8 lost on wall to reasoning tokens; same
  risk for Laguna/K2. Truncated-turn retry is ON (bench default) — watch its
  firing counts.
- **Weekend overrun** — R1 alone is decisive enough to report on if R2 can't
  finish; R2 trims to one survivor before it trims reps.
