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
