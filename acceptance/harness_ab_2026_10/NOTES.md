# harness_ab_2026_10 run notes

- 13:54:13–13:57:38 2026-10-18: Claude Code probe (one fixture, separate work dir) ran concurrently with the queue.
  Overlapped: opencode/champ/rep1 nothing-ever-happens-document-config, qwen/champ/rep1 fixtures 1–2.
  Wall times of those three cells are contended; treat their wall_s as unreliable.
- 13:58 queue restarted at 5d7a0f3 to add the claude harness (resume skipped done fixtures; qwen in-flight fixture re-ran).

## 2026-10-18 17:07 — queue stopped, two arms added (user request), relaunched 17:13

- Stopped at a fixture boundary (qwen/q38/rep1 at 6/10; [7/10] isomer-quickstart
  had just started and re-runs from scratch). Probes then ran alone on the box
  17:08–17:12 (no overlap with queue cells).
- luxe-offline/champ: luxe maintain, deps-audit x3, same loopback-only sandbox,
  cve_lookup disk cache denied (verified: a fresh entry reads as a miss; OSV
  ConnectError EPERM). Runs first in the relaunched queue.
- codex-cat: Codex with a model_catalog_json entry (its gpt-5.5 entry
  re-pointed) + shim custom-tool translation so apply_patch reaches the model
  (oMLX drops custom tools). Probe in _probe_codex_cat/ (champion,
  isomer-healthcheck + lpe-typing): 7 apply_patch calls, 3 applied, 4
  rejected by the Codex verifier as malformed.

## 2026-10-19 05:40 — cve_cache denied for outside harnesses (user decision)

Glimmer rep1's Claude Code deps-audit subagent read `~/.luxe/cve_cache`
(report used none of it). 9c0de33 adds a read/write deny on the cache to the
outside-harness offline profile, matching the luxe control. Pulled on m5 at
05:39:35; rep2 champ codex-cat had started at 05:39:22 with the old profile,
so its `_env/offline.sb` was appended in place at 05:40 (original kept as
`offline.sb.pre-cvedeny`) — sandbox-exec reads the profile per fixture, and
only the first fixture (typing) had launched. Every cell before it (rep1 all,
rep2 champ opencode/qwen/codex/claude) ran without the deny. Verified on m5:
`cat`/`ls` of the cache → "Operation not permitted" under the profile.
