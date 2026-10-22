# 2026-10 refactor and tech-debt pass — results

This is the second whole-project review. The first closed on 2026-09-27; since then about 50 code commits added roughly 5,000 lines.
Four read-only reviewers covered the agent core, chat and kit, gitkit/web/MCP, and the bench/scripts/tests. Their findings
landed as seven PRs, each one a signed fast-forward to main. The ruleset blocks GitHub-side merges.

## What landed

| PR | Scope | Main commits |
|---|---|---|
| #33 | Chat bugs: compare's bare side, TUI freeze, startup hang, `/pull` engine refusal, smoke fix strings | `16dafa6..ef795dc` |
| #34 | Bench: regrade the run's own commit, archive the harness bake-off, lint | `6c50cc9..bb087a1` |
| #35 | Remove 7 refuted or inert off-by-default levers, plus dead state and stale comments | `..3096dfe` |
| #37 | gitkit/MCP/web: break the import cycle, prompts to the registry, decompose the three hotspots, 2 bugs | `7c3ab4d..5b15e1d` |
| #36 | Tests and CI: stub host probes, CI extras parity (`[pdf]` tests now run), faster test servers, lint | `990e231..7292ad5` |
| #39 | Chat structure: `chat/doctor.py`, `chat/supervisor.py`, `StartupFlags`, one outcome renderer, 2 drift fixes | `cc6bb48..43f650d` |
| #38 | Decompose `run_agent` (C901 95 → 12), 9 new golden trajectories, maintain prompts to the registry | `4a3459a..385daf4` |

## Bugs fixed (each with a regression test seen RED on the old code)

- **compare's bare side** left `LUXE_CTX_CAL_TOOLS` and `LUXE_COMPACT_CACHE_FRIENDLY` on and still set the removed
  `LUXE_PROSE_BURST`. The test now derives the expected levers from `flags.py`, so a new lever fails CI until the
  bare side turns it off.
- **TUI froze for up to about 2.5 minutes after an aborted turn.** Kit recovery, which can restart oMLX, ran on the UI
  thread. It now runs on the turn worker, and the result travels on `TurnOutcome.abort`.
- **Startup could hang for 10–40 minutes on a wedged endpoint.** The origin and model-capability probes inherited
  the generation-sized read timeout. They now use the 4 s liveness timeout through an additive `timeout_s=` argument;
  the bench wire is unchanged.
- **`/pull` would drive oMLX admin calls at an OpenRouter or llama-server URL.** It now shares `luxe pull`'s engine refusal.
- **`luxe smoke` printed a weights fix that cannot reach HuggingFace.** It now prints doctor's fix.
- **`regrade_local.py` graded `origin/<branch>`, but bench branch names get reused.** `run.py` now records
  `head_sha_used` plus a `diff.patch`, and regrade grades that commit. Old runs are flagged ATTRIBUTION UNVERIFIED.
- **MCP:** `luxe_summarize` and `luxe_explain` skipped the audit entry when no runner was configured.
- **gitkit apply** injected `survey_notes.md` from a map saved at a different HEAD.
- **Doctor's no-project path** dropped the `/write` fix. **The REPL's git-analysis hook** got no sub-prompt reader.
- **Host coupling in tests:** about 34 tests ran the real `pgrep`/`lsof`, and the Keychain read was cached in an
  order-dependent way. That was the `test_chat_smoke` failure on m1 while its oMLX was stale. An audit hook now
  refuses those spawns outside tests marked `host_probes`.
- **The 49 `test_mcp_pdf.py` tests ran nowhere.** CI now installs `[pdf]`.

## Removed (the user approved all seven)

`LUXE_POST_WRITE_IDLE_REPEATS`, `LUXE_REPEAT_READ_STUB`, `LUXE_REREAD_NOTE`, the `commit_first` prompt variant,
`LUXE_EARLY_BAIL_TRAJECTORY_SHAPE`, `LUXE_EARLY_BAIL_COMMIT_ONLY` and `LUXE_LOAD_PRIORS` (cohort priors). They are
recorded in agents.sdd under "Removed mechanisms (2026-10-22 refuted-lever cleanup)". Also removed:

- **Unused code:** the swarm/phased config relics (`profile`, `task_types`), 46 unread `deep` re-exports, and two
  dead TUI helpers.
- **Archived, not deleted:** the harness bake-off driver, now in `scripts/_archive/` with its tests in `tests/_archive/`.

## Complexity (ruff C901)

| Function | Before | After |
|---|---|---|
| `run_agent` | 105 (95 after the lever removal) | 12 (`backend.chat` must stay inline per chat.sdd) |
| `run_deep_report` | 61 | every function ≤ 10 |
| `run_doctor` | 32 | 1 |
| `run_git_report` | 28 | 14 |
| `run_apply` | 27 | every function ≤ 10; Ctrl-C revert state on one object |
| `_run_interactive` | 27 | 4 |

Still above 25: `maintain_pipeline` (55), `maintain_suite.run.main` (50) and `backend._stream_once` (28). The other
three over 25 are vendored benchmark fixtures. `maintain_pipeline` and the bench `main` were deferred on purpose:
both are on the benchmark path and were not part of this pass.

## Benchmark verification (m5, `review_2026_10_ab/`, a local gitignored directory)

Champion `Qwen3.6-35B-A3B-6bit`, all 10 maintain_suite fixtures, 3 reps, with the two arms interleaved in each rep.

- **ctl** = `bb087a1`: main before the lever removal.
- **trt** = `780340e`: the lever removal plus the `run_agent` decomposition. Its bench-path files are byte-identical to
  the landed `385daf4`.

| | ctl | trt |
|---|---|---|
| expected_outcome | **30/30** | **30/30** |
| score | 120 | 120 |
| byte-identical diffs | 9/10 fixtures identical in all 6 runs | |
| gates triggered | 0 | 0 |
| max-steps bailouts | 2 (both document-config) | 1 (document-config) |
| wall (sum) | 1,337 s | 1,226 s |
| prompt tokens | 3.79 M | 3.56 M |

- **document-config** varies in both arms, as it always has. trt rep3 reproduces ctl's diff byte for byte, and the
  two new trt docs both hand-verify as REAL: every `PM_RISK_*` default and the `DRY_RUN`/`BOT_MODE` defaults match
  the fixture source.
- **The differences in wall time and tokens all come from document-config.** On every other fixture, prompt tokens
  match run for run; deps-audit alternates 80k/93k in both arms.
- **Verdict:** no harm. The decomposition is behaviour-preserving on the bench.

## Smoke after deploy (main `385daf4`)

`luxe ready` and `luxe smoke --chat --code` both returned READY on all three hosts:

| Host | Smoke time | Notes |
|---|---|---|
| m1 | 141 s | |
| m5 | 12 s | |
| neo | 50 s | Only after re-bootstrapping its llama-server LaunchAgent. It had not run since a reboot 7 days earlier — the known Background Task Management gap in CLAUDE.md, not a code issue. |

## Left open

- Clean candidates for a future pass: `maintain_pipeline` (55), `maintain_suite.run.main` (50),
  `backend._stream_once` (28), and splitting `grade.py`'s `check_*` gates into a module.
- **neo boot persistence** still needs the one-time approval in System Settings → Login Items.
- **The uv.lock drift** is still uncommitted on all three hosts.
- **The `_git` test helper** has 15 copies, a consolidation skipped as too wide for this pass.
