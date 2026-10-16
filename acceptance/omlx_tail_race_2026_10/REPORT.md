# oMLX 0.7.0 — cache-freshness wait skips sub-block tail stores

**Date:** 2026-10-16 · **Host:** m1 (M1 Max 64 GB, oMLX 0.7.0 brew,
`Qwen3.6-35B-A3B-4bit`) · **Status:** patched LOCALLY on m1; **already fixed
upstream** (jundot/omlx#4492, fix #4428 + c756df2, both in `v0.7.1rc1`) — not
re-filed.

## The bug

For hybrid models (Qwen3.6: GatedDeltaNet `ArraysCache` layers) oMLX can
resume a prompt only at boundary snapshots — every `paged_cache_block_size`
tokens plus a *tail-terminal* snapshot at the generation-prompt start. The
store that publishes them runs asynchronously after a request finishes.

`Scheduler._find_relevant_inflight_store` decides whether a newly admitted
request should wait for such a store. It measures the reusable overlap in
whole blocks only:

```python
restorable = (common // block) * block
...
if restorable < block:
    continue
```

so a store whose sequence is shorter than one block is never waited on, even
though its tail snapshot is exactly what the next request would restore. An
agent's next step arrives milliseconds after the previous response, before
the store lands, and prefills from zero.

The block is `max(2048, prefill_step_size, qwen35_prefill_floor)`
(`_enlarge_block_size_for_arrays_cache`). `_detect_qwen35_prefill_floor`
returns 4096 on ≥64 GB hosts **without** NAX — m1 (M1 Max) gets 4,096-token
blocks, m5 (M5 Max, NAX) gets 2,048. So on m1 every agent step whose previous
prompt was under 4,096 tokens — every early step of a `luxe chat`/`luxe code`
turn (system + tools alone is 2.6–3.0k) — races the store. m5's bench never
showed it because its maintain prompts start above 2,048.

## Evidence

`race_probe.py`: A = `[system(uuid nonce + filler), user]` with
`max_tokens=4`; B = A + assistant + user, sent the instant A returns.

| condition (m1, 4bit) | B misses |
|---|---|
| A ≈ 2,985 tokens, B immediately (stock 0.7.0) | **12/12** |
| A ≈ 2,985 tokens, B after a 1.0 s pause (stock) | 0/6 — restores `cached=2981 suffix=25` |
| A ≈ 5,925 tokens, B immediately (stock) | 0/6 — waits, restores the full tail (5,918) |
| A ≈ 2,985 tokens, B immediately (**patched**) | **0/12** |
| A ≈ 5,925 tokens, B immediately (patched) | 0/4 |

Historical m1 logs (pre-probe): continuation-step misses 35% (34/98) when
the previous prompt was <4,096 tokens vs 0.8% (15/1,931) when ≥4,096 — lower
than the probe because real steps include tool time that sometimes lets the
store land.

## The fix (`omlx-tail-race.patch`)

Three hunks: `_InflightStoreInfo` records the store's
`tail_terminal_token_count` (already on the `_BoundarySnapshotProvider`);
`_find_relevant_inflight_store` counts that tail as restorable when the
prompt extends past it (`tail <= common and tail < len(prompt)`), and only
skips a sub-block store when it has no usable tail. The existing timeout,
store/overlap ratio and min-ratio heuristics are unchanged.

After the patch on m1: probe 0/12, the ≥4,096 control unchanged, `luxe smoke`
READY, `luxe smoke --chat --code` READY.

## Local state + how to undo

- Patched file: `/opt/homebrew/Cellar/omlx/0.7.0/libexec/lib/python3.11/site-packages/omlx/scheduler.py`
- Original kept beside it: `scheduler.py.orig-race-2026-10-16`
- Undo: `cp <that>.orig-race-2026-10-16 <that>` then `brew services restart omlx`.
- **A `brew upgrade omlx` / reinstall silently drops the patch.** An upgrade
  to **0.7.1 or later is the intended exit** — it carries the upstream fix. A
  reinstall of 0.7.0 is the only way to lose the fix. Either way re-run
  `race_probe.py` after (`uv run python race_probe.py Qwen3.6-35B-A3B-4bit 12
  1800 0`; any MISS = the race is back).
- m5 is not patched (its server is parked, and 2,048-token blocks make the
  race rarer there — only prompts under 2,048).

## Upstream status (checked 2026-10-16)

Already reported as jundot/omlx#4492 (2026-10-12, same root cause, 4,096-token
block on an M2 Max) and closed 2026-10-13 by #4428 ("admit sub-block prompts so
the freshness gate defers for a tail" — maintainer measured 0/12 → 12/12 hits)
plus c756df2 (sub-block requests ended by a parser/stop string were never
stored). Both are in `v0.7.1rc1`; Homebrew stable is still 0.7.0. Our draft
below was therefore not filed; kept for the record.

### Draft (superseded — not filed)

> **Cache-freshness wait skips sub-block tail stores, so the next agent step
> misses the prefix cache (hybrid models)**
>
> `_find_relevant_inflight_store` computes `restorable = (common // block) *
> block` and skips stores with `restorable < block`. For ArraysCache hybrid
> models the store ends in a tail-terminal boundary snapshot off the block
> grid, which the next request can restore — but when the stored sequence is
> shorter than one block (4,096 on non-NAX ≥64 GB hosts via the Qwen3.5
> prefill floor) the request is admitted before the async store lands and
> prefills from zero. Repro: send A (~3k tokens, `max_tokens=4`), then
> immediately B = A + assistant + user: 12/12 B requests report
> `cached_tokens=0`; with a 1 s pause, 0/6. Prompts over one block are waited
> on correctly. Proposed fix: record `tail_terminal_token_count` in
> `_InflightStoreInfo` and treat it as restorable when the prompt extends past
> it (patch attached, 3 hunks). Verified 0/12 after.
