# Overnight comparison session — 2026-04-26 → 2026-04-27

End-to-end report on the oMLX vs Ollama vs LM Studio comparison run, the bugs uncovered along the way, and the harness improvements that shipped. Written so a fresh session can pick this up cold.

## TL;DR

- **Decision:** Stay on oMLX for the review/refactor agents (Qwen2.5-32B-Instruct-4bit). Confidence: **high**.
- **Numbers:** oMLX is **21.7% faster** than Ollama on real multi-turn /review wall (median 36.4m vs 46m), and **1.43× faster** on synthetic decode. Both metrics now agree.
- **LM Studio comparison:** unusable. Qwen 32B loops on identical tool calls inside long-context multi-subtask runs. Reproducible only inside the agent loop — three isolated probes against the same backend all succeed. Documented as a backend-quality finding.
- **5 commits landed** (`a5c2185 → 075670a`) covering bug fixes, agent-loop loop guard, harness recovery + aggregator pipeline, ops scripts, and cold-cache verdict dedupe.

## The original problem

Session opened with an MLX/Metal GPU crash report from a 12-hour comparative testing run (`mlx::core::gpu::check_error` aborting the OMLX server). brew launchd KeepAlive recovered the daemon automatically, but the harness silently produced empty data for the multi_turn_reviews phase because nothing checked backend liveness mid-run. That kicked off the broader review of the testing infrastructure.

## Bugs found and fixed

### 1. Path-vs-URL in multi_turn_reviews (commit `e4f1a09`)
`run_overnight.py` passed the local repo path to `start_review_task`, which expects a URL. `cli/git.py:resolve_repo` then derived a target path from the "URL" (interpreting the path as one), saw the path existed, and bailed with `target path X exists but origin does not match X` for every (repo, backend). **Symptom:** every multi_turn sub-chunk in the prior overnight ran 0 subtasks before this fix. **Fix:** pass the URL from the `REPOS` registry in `run_overnight.py`.

### 2. LM Studio missing from `_BACKEND_OVERRIDE_URLS` (commit `a5c2185`)
`cli/backend.py` had `ollama`, `omlx`, `llamacpp` but no `lmstudio`. `LUXE_BACKEND_OVERRIDE=lmstudio` silently fell through to the agent's default endpoint (`:8000` = oMLX), which 404'd on the LM Studio model tag. **Symptom:** every lmstudio sub-chunk got "404 Not Found at :8000". **Fix:** add `"lmstudio": "http://127.0.0.1:1234"`.

### 3. Backend override redirected URL but not model name (commit `a5c2185`)
`agents.yaml` hardcodes the model with the oMLX-internal name (`Qwen2.5-32B-Instruct-4bit`); Ollama and LM Studio serve the same weights under `qwen2.5:32b-instruct` and `qwen2.5-32b-instruct` respectively. Without a model rename, redirecting just the URL hits the wrong tag. **Fix:** added `LUXE_MODEL_OVERRIDE` env var and per-backend translation in `run_overnight.py`.

### 4. Planner misrouted to the workload backend (commit `a5c2185`)
The planner uses a tiny router model (`qwen2.5:7b-instruct`) reliably tagged only on Ollama. When `LUXE_BACKEND_OVERRIDE=omlx` redirected the workload agent, `make_backend()` honored the override for **every** call — including the planner — and the planner's request to oMLX returned text the JSON extractor couldn't parse, falling back to a 1-subtask degenerate plan. **Symptom:** oMLX runs produced 1-subtask plans vs Ollama's 6+. **Fix:** new `ignore_override=True` keyword on `make_backend`; planner uses it.

### 5. State.json overwrites lost cross-(repo, backend) data (commit `e4f1a09`)
Each `--only multi_turn_reviews` invocation overwrote `state.json`'s `result.runs` with just that sub-chunk's record. `composite_verdict.py` read from there → reported "no-data" even though all the runs were durable on disk in `~/.luxe/tasks/T-…`. **Fix:** new `aggregate_multi_turn.py` walks task records, joins by chronological proximity to `multi_turn_reviews.log`, writes `multi_turn_runs.jsonl`. Composite verdict reads that preferentially.

### 6. Cold-cache vs warm-cache dedupe bias (commit `075670a`)
Aggregator dedupe rule kept the LATEST run on ties, biasing toward warm-cache retries. elara × ollama had a 56-min cold first run plus a 31-min warm retry; previous rule kept the warm one, making the median unfair to oMLX (which always cold-starts under launchd KeepAlive). **Fix:** tie-break on EARLIEST `started_at`. Verdict flipped: oMLX +15.6% (medium) → −21.7% (high).

### 7. Agent loop tool-call loops (commit `ce3b831`)
LM Studio's Qwen 32B repeats the same `(tool, args)` call up to 20 times until step budget runs out. Ollama and oMLX never trip this. **Fix:** signature each call; after `LOOP_REPEAT_LIMIT=3` identical calls, refuse the 4th with an in-band ERROR result instructing the model to stop. Earlier nudge-via-system-message version proved ineffective — the refuse-and-error version is in but only smoke-tested with the weaker version (3/7 subtasks done vs 2/7 baseline). LM Studio re-run with the refuse version was opted out of.

## Verdict (final)

| Agent | Current | Recommended | Confidence | Reasoning |
|---|---|---|---|---|
| code | oMLX | (insufficient data) | no-data | oMLX_VERDICT missing for code candidate. |
| **review/refactor** | oMLX | **oMLX** | **high** | **oMLX 36.4m vs Ollama 46m, Δ = −21.7%. Decode 1.43× faster. Per-bench wins corroborate.** |
| writing | (unchanged) | (deferred) | no-data | Gemma 3 27B not in candidates.yaml as MLX-served. |
| calc | (test produced data) | enable DFlash if decode_throughput shows ≥1.5× | medium | Phase 4 sweep ran successfully. |

Per-repo multi-turn /review wall (post-fix data only):

| Repo | Ollama | oMLX | Δ |
|---|---|---|---|
| elara | 56.0m (cold) | 50.5m | −10% |
| never-say-yes | 46.4m | 36.4m | −21% |
| neon-rain | 21.8m | 13.5m | −39% |
| **median** | **46.0m** | **36.4m** | **−21%** |

LM Studio: every sub-chunk failed. Best post-loop-guard result was 3/7 subtasks done on neon-rain.

## Harness improvements (durable infrastructure)

- **Per-phase backend probes** (`_probe_backend`, `_wait_for_backend`) before each non-preflight phase. preflight may have run hours ago; oMLX could have crashed since. Probe waits up to 5 min for launchd KeepAlive to recover.
- **Sanity guard on multi_turn_reviews:** when a /review task completes with `wall_s<60` and `subtasks_done==0`, re-probe and record `status="backend_died_mid_run"` instead of letting it look clean.
- **`--only PHASE`, `--repo NAME`, `--backend NAME`** CLI flags on `run_overnight.py` so a single (repo × backend) chunk can be supervised on its own.
- **`scripts/run_overnight_supervised.sh`** — interactive walk through the 6 phases with confirmation prompts.
- **`scripts/run_overnight_catchup.sh`** — unattended re-runner for slots that produced no usable data.
- **`scripts/tail_progress.sh`** — live overview refreshing every 5s with current step + per-subtask progress.
- **`scripts/aggregate_multi_turn.py`** — durable cross-(repo, backend) data view.
- **`scripts/probe_lmstudio_{tools,review,stream}.py`** — three diagnostic probes that confirmed the harness wire format is correct (the bug is downstream in LM Studio).
- **Synthetic_baseline budget bumped** from 90 → 120 minutes; previous run hit the 90-min limit at 95% data captured.

## Open items for the next session

1. **LM Studio Qwen 32B tool-call loop** — confirmed reachable only inside the agent loop's long-context multi-subtask conversation. Three probes show the wire format is fine. Investigate at:
   - The GGUF chat template in whatever Qwen 32B Q4 is loaded
   - LM Studio's OpenAI-compat shim under accumulated `assistant + tool_calls + tool_results` history
   - Try a different LM Studio model (Llama 3.3 70B, etc.) to isolate per-model vs systemic
2. **Re-run LM Studio with the refuse-version loop guard** if revisiting LM Studio. Smoke test: `neon-rain × lmstudio`. Decision criterion: ≥4/7 subtasks done with `refused:` count > 0 in the task log → fan out to all 3 repos.
3. **Plan caching** — planner is non-deterministic at `temperature=0.1`, so cross-backend comparisons are "same goal, possibly slightly different decomposition" rather than "identical plan, two backends." Architectural fix: cache plans per-(repo, mode) and reuse across (repo × backend) variants.
4. **Synthetic_baseline llamacpp slot schema** — the patched `qwen2.5-32b × llamacpp × prefix_cache_decay` jsonl has 30 rows but a quick schema check showed `candidate / backend / bench` reading None. Re-verify column names before relying on it for the lone llamacpp data point in the verdict.
5. **MLX/Metal `gpu::check_error` crash** — the original session driver. Latent. Workaround: `brew services restart omlx` proactively, or schedule daily restarts. Upstream fix lives in Apple's Metal driver / mlx-lm; not a luxe bug.

## Where to look in a new session

- **Verdict reports:** `luxe/results/overnight_2026-04-26T11-46-44/{COMPOSITE,VERDICT,SPEC_DECODING_VERDICT}.md`
- **Multi-turn data:** `luxe/results/overnight_2026-04-26T11-46-44/multi_turn_runs.jsonl` and per-task records under `~/.luxe/tasks/T-2026042{6,7}*`
- **Memory:** `~/.claude/projects/-Users-michaeltimpe-Downloads-luxe/memory/` — `MEMORY.md` is the index, individual files cover each finding/preference
- **Recent commits on this branch:** `git log --oneline a5c2185..HEAD` (5 commits)
- **Probe scripts** (re-runnable for the LM Studio investigation): `luxe/scripts/probe_lmstudio_*.py`
- **Operational scripts:**
  - `bash scripts/run_overnight_supervised.sh` — interactive
  - `nohup bash scripts/run_overnight_catchup.sh > catchup.log 2>&1 &` — unattended
  - `bash scripts/tail_progress.sh` — live monitor
- **Harness control flow:** `luxe/scripts/run_overnight.py` (phases, probes, sanity guards)
- **Agent loop guard:** `luxe/cli/agents/base.py` (search for `LOOP_REPEAT_LIMIT`)
