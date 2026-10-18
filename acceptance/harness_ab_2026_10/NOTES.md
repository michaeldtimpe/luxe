# harness_ab_2026_10 run notes

- 13:54:13–13:57:38 2026-10-18: Claude Code probe (one fixture, separate work dir) ran concurrently with the queue.
  Overlapped: opencode/champ/rep1 nothing-ever-happens-document-config, qwen/champ/rep1 fixtures 1–2.
  Wall times of those three cells are contended; treat their wall_s as unreliable.
- 13:58 queue restarted at 5d7a0f3 to add the claude harness (resume skipped done fixtures; qwen in-flight fixture re-ran).
