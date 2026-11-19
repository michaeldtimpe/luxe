# DaisyRose-27B probe (2026-11-19, m5): aborted after rep 1, not promoted

## What and why

DaisyRose-27B (RadixArk, released 2026-11-18) is an agentic-RL fine-tune on a 27B
Qwen base. The config is `Qwen3_5ForConditionalGeneration`, text model `qwen3_5_text`,
64 layers, hidden 5120: the same architecture as Qwen3.6-27B, not the "Qwen3.8" the
release notes suggested. License apache-2.0, chat template published, no independent
benchmark results found. The question was whether the RL fixed the write-avoidance the
Qwen 27B line showed on `document-config` (Qwen3.8-27B, 2026-08-18: 0-1/3).

Agreed probe (memory reminder, set 2026-11-19): `nothing-ever-happens-document-config`,
3 reps on m5, record tok/s. Abort below 3/3 or above 1.5x ThinkingCap's wall on that cell.

## Setup

- Weights: `mlx-community/DaisyRose-27B-4bit` (uniform 4-bit g64, publisher allowlisted),
  pulled with `luxe pull`, template pinned, lint clean. RadixArk itself is not on
  `trusted_publishers`.
- Harness: maintain_suite, main `78f6755`, oMLX 0.8.0, shipped defaults,
  `run_arms.sh DaisyRose-27B-4bit daisyrose-4bit 3 probe=` with
  `IDS=nothing-ever-happens-document-config`.
- Reference: ThinkingCap-Qwen3.6-27B-mlx-6Bit on the same cell
  (`acceptance/thinkingcap_27b_2026_10/r1`): 3/3 pass, wall 522 / 289 / 309 s, mean 373 s,
  so the abort line is 560 s. Note the bias favors DaisyRose: a 4-bit against a 6-bit.

## Result

| rep | pass | score | wall | tokens in/out | gen tok/s | diff |
|---|---|---|---|---|---|---|
| 1 | FAIL | 1/5 | 975 s | 116k / 26k | ~27 | none |

Both abort conditions tripped on rep 1 (3/3 no longer possible; 975 s is 2.6x the
reference), so reps 2 and 3 were stopped.

Trajectory (run `6ca6a8748c7f`): 15 read-only tool calls over steps 0-9 (11 `read_file`,
2 `grep`, 1 `bm25_search`, 1 `list_dir`), no write. Write pressure fired at step 10.
Steps 10, 11 and 12 each hit the 8,192-token cap with no tool call; the truncated-turn
retry fired twice, and the final turn ended empty with 64,543 reasoning characters and no
answer. Same failure shape as the Qwen3.8-27B run: it reads, then deliberates
past the turn cap instead of writing.

## Verdict

Not promoted. Do not re-open without new information (an independent agentic result,
or a release note that addresses reasoning length). Classification: model_behavior.
