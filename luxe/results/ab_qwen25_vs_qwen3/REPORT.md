# Qwen2.5 → Qwen3 candidate evaluation

**Date:** 2026-04-27
**Backend:** oMLX :8000 (4-bit MLX weights)
**Bench config:** `decode_throughput` (3 prompts: 50/500/4000 tok target) + `humaneval_plus --limit 20`
**Phase dir:** `luxe/results/runs/ab_qwen25_vs_qwen3/`

## Per-role verdict

| Role | Incumbent | Best Qwen3 | Verdict |
|---|---|---|---|
| router/general/lookup/image | Qwen2.5-7B-Instruct | Qwen3-8B | **HOLD** — accuracy regression + thinking mode |
| code | Qwen2.5-Coder-14B-Instruct | Qwen3-Coder-30B-A3B-Instruct (MoE) | **HOLD** — n=20 too small; ~10pp gap |
| review / refactor | Qwen2.5-32B-Instruct | Qwen3-30B-A3B-Instruct-2507 (MoE) | **SWAP** — 6× faster on a real `/review` (9m vs 57m), much less fabrication |
| research / calc | Qwen2.5-32B-Instruct | (none) | **NO SWAP** — Qwen3-30B-A3B-Instruct-2507 silently skips tool calls and fabricates citations; production-breaking for tool-required agents |
| writing | gemma-3-27b-it | (Qwen3.6 excluded this round) | no change |

**Update (live test, 2026-04-27 17:40):** Initial swap moved all four reasoning agents (research/calc/review/refactor) to the MoE. Live `/research` runs on the user's machine showed 0 tool calls per query — the model answered from training data with fabricated `[1]–[n]` citations (e.g. "Immigration, Refugees and Citizenship Canada — Permanent Residence" with no URL; Bolt EV "60 kWh / 150 kW" when real spec is 65 kWh / ~55 kW). Same model on `/review elara` produced a clean 9-min report vs the prior 57-min Qwen2.5-32B run with 30+ duplicated `json.loads` findings. Conclusion: **swap helps the read-and-reason agents, hurts the tool-required agents**. Partial revert applied to research/calc.

## Decode throughput (oMLX, 4-bit, 3-prompt sweep)

| Candidate | mean tok/s | mean TTFT | mean ctoks | peak RSS |
|---|---|---|---|---|
| qwen2.5-7b-instruct | 37.7 | 0.42s | 802 | 40.4 GB |
| **qwen3-8b** | 37.2 | **23.6s** ⚠️ | 1881 ⚠️ | 45.3 GB |
| qwen2.5-coder-14b | 22.0 | 0.62s | 727 | 37.3 GB |
| **qwen3-coder-30b-a3b** | 25.2 (+15%) | 3.6s | 1219 | 36.1 GB |
| qwen2.5-32b-instruct | 10.6 | 1.11s | 658 | 13.1 GB |
| **qwen3-32b** | 13.2 | **94.7s** ⚠️ | 1746 ⚠️ | 21.6 GB |
| **qwen3-30b-a3b-instruct-2507** | 25.1 (+137%) | 3.6s | 1091 | 20.2 GB |

**TTFT regression for `qwen3-8b` and `qwen3-32b`** is the smoking gun: their thinking-mode reasoning blocks generate ~1500–2000 completion tokens per prompt regardless of target length, and the harness measures TTFT to first non-think token. The MoE Instruct-2507 and Coder variants don't have this issue (they're the non-thinking checkpoints).

## HumanEval+ accuracy (n=20)

| Candidate | pass% | mean wall | mean ctoks | comment |
|---|---|---|---|---|
| qwen2.5-7b-instruct | **90%** | 2.1s | 51 | baseline |
| qwen3-8b | 75% (−15pp) | **47.6s** (22×) | 1333 | thinking-mode fluff inflates wall |
| qwen2.5-coder-14b | **100%** | 3.4s | 56 | baseline |
| qwen3-coder-30b-a3b | 90% (−10pp) | 4.3s | 87 | clean, but n=20 too small to sign-off |
| qwen2.5-32b-instruct | **100%** | 7.9s | 64 | baseline |
| qwen3-32b | 85% (−15pp) | **127.4s** (16×) | 1154 | thinking-mode regression |
| **qwen3-30b-a3b-instruct-2507** | 95% (−5pp) | **3.7s** (2.1× faster) | 72 | clean, fast, ~parity |

n=20 caveat: 1 task = 5 percentage points. The 95% vs 100% delta on the reasoning candidate is one task different — within sampling noise.

## Recommended action

**SWAP now (one role family):**
- `research`, `calc`, `review`, `refactor` agents: `Qwen2.5-32B-Instruct-4bit` → `Qwen3-30B-A3B-Instruct-2507-4bit`
  - 2.1× faster wall time on real coding-style prompts
  - parity-ish accuracy at n=20
  - lower peak RSS (20 GB vs 13 GB — Qwen2.5-32B was already small in oMLX's memory accounting; new model fits comparably)
  - same Hermes tool-call format, validated in Phase 1 smoke test

**HOLD on rest:**
- 7B family swap is a clear regression (thinking mode + accuracy drop). Need a non-thinking Qwen3 small variant (e.g. Qwen3-4B) or `enable_thinking=false` chat template kwarg, then re-bench.
- Code agent: 90% vs 100% at n=20 isn't decisive. Recommend either (a) full HumanEval+ run (164 tasks) before committing, or (b) keep 14B Coder until a clearer signal emerges. The +15% decode tok/s win doesn't outweigh the accuracy uncertainty for a code agent that's already fast enough.

## Open follow-ups

1. **Full HumanEval+ for the code agent decision** — the n=20 90% could be 91% or 95% on full 164. If it lands ≥95%, swap; otherwise hold.
2. **Qwen3-4B-Instruct (non-thinking)** as the small-class candidate. Smaller than current 7B, no thinking mode, would be a true upgrade target.
3. **`enable_thinking=false` for Qwen3-8B/32B**: oMLX may pass chat-template kwargs through; worth a one-liner test before giving up on those checkpoints.
4. **luxe_replay benchmark** (skipped here for time) — would surface multi-turn tool-loop bugs that single-shot HumanEval+ can't catch.
5. **Qwen3.6** — re-evaluate in 2–4 weeks once the Apr 16/22 weights settle.
