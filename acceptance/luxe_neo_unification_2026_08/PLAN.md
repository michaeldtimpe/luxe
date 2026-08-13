# neo unification — LaunchAgent persistence fix + micro-mind retirement (2026-08-13)

**Executor:** Opus 5 agent from m5, driving neo over ssh; luxe code/doc work
happens in this checkout (`~/Downloads/luxe` on m5).
**Reviewer:** Fable (main session) independently checks the work after the
report lands. Nothing merges to `main` and nothing is pushed to
`origin/main` before that review. Work on a branch: `feat/neo-unification`.

**Mandate (user-approved):**
1. Fix neo's llama-server boot-persistence gap (found 2026-08-13: the
   `com.micromind.llama-server` LaunchAgent had been down for 8 days since
   last boot — plist present, never loaded, port dark).
2. Retire micro-mind. luxe becomes the one agent codebase fleet-wide; neo
   keeps `llama-server` + the GGUF champion (`qwen3-4b-instruct-2507`
   Q4_K_M, ctx 16384, q8 KV) as its engine. Evidence base: MLX probe
   2026-08-13 (G-RAM not-viable / G-DRILL pass —
   `acceptance/mlx_neo_probe_2026_08/REPORT.md`) and the recorded
   `luxe smoke --code` pass through neo's router (micro-mind lessons.md
   2026-08-03: 8 steps, 10 tool calls, pytest green, 41s).

## Pre-registered gates

- **G-BOOT**: after a real reboot of neo with ZERO post-boot ssh
  intervention, `curl localhost:8080/v1/models` serves the champion within
  5 minutes of the box answering ssh. This is the only honest test of a
  persistence fix. Guarded by P1.1 (FileVault / auto-login preflight) —
  if a headless reboot would strand the box at pre-boot auth, DO NOT
  reboot; mark G-BOOT deferred-needs-user and verify by `launchctl print`
  state evidence instead.
- **G-LUXE-LOCAL**: on neo, against the LOCAL llama-server (not m5):
  `luxe ready` exits 0, and real agentic drills pass — the
  `luxe smoke --chat --code` bar: read-only file-grounded answer, plus
  fix-a-planted-bug with pytest green and the expected diff, tool calls
  round-tripping natively.
- **G-FLEET-NO-REGRESSION**: full pytest suite green on m5 at the branch
  tip; benchmark/maintain path untouched (single-champion pin, TOOL_FNS,
  `single_64gb.yaml` unchanged); `luxe ready` still green on m5. m1/m4
  untouched entirely.
- **G-DOCS**: CLAUDE.md fleet-sibling section + chat.sdd neo pins updated
  to the new reality; micro-mind marked superseded the same way
  neo-llm-bench was (its 2026-08-03 pointer commit `d6f32321` is the
  precedent); micro-mind's llama-server/router runtime config explicitly
  EXCLUDED from retirement (it is now neo's engine infrastructure).
- **G-REVIEW-READY**: all luxe changes committed on `feat/neo-unification`
  (branch may be pushed to origin as a branch; `main` untouched);
  micro-mind's retirement commit prepared on neo but NOT pushed; report
  contains pasted evidence for every gate.

## Part A — boot persistence (do this first)

### P1 — Diagnose before choosing a fix
1. Preflight the reboot question first: `fdesetup status` (FileVault),
   `defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser`
   (auto-login), `sudo -n true` (non-interactive sudo available?). Record
   all three — they decide which fixes are even on the table.
2. Establish why the agent didn't load: is neo headless (no GUI login
   session after reboot → `gui/$UID` domain never bootstraps)? Check
   `launchctl print gui/$UID` existence, `last reboot`, log show for the
   label around last boot. Don't guess — write down the mechanism.
3. Check whether the plist is tracked anywhere (`~/dotfiles` repo — the
   router config `luxe/neo-models.ini` lives there); if so, the fix lands
   in dotfiles too, not just on disk.

### P2 — Fix, matched to the diagnosis
Preference order (pick the first that fits the facts from P1):
1. **Auto-login + LaunchAgent** (no sudo needed beyond loginwindow
   defaults, keeps gui domain): only if FileVault is OFF.
2. **Convert to a LaunchDaemon** (`/Library/LaunchDaemons`, `UserName`
   key, absolute paths, no `$HOME` assumptions): needs sudo; survives
   reboot with no login at all. Keep the label lineage documented.
3. If neither is executable non-interactively, implement the best
   available partial (e.g. corrected plist + documented one-command
   post-boot bootstrap in OUTAGE-style docs) and mark G-BOOT
  deferred-needs-user with the exact remaining step.
Also: point `KeepAlive` at the right semantics (crash restart), make sure
stdout/err log paths exist and rotate sanely, and `launchctl kickstart`
proves a manual crash recovers.

### P3 — Verify (G-BOOT)
1. If reboot is safe per P1.1: `sudo shutdown -r now` (or `reboot`), wait,
   ssh back, verify the server came up with zero intervention; paste
   timeline + curl output. Re-run twice only if the first result is
   ambiguous.
2. Regardless of reboot: `launchctl print` state, `kickstart` recovery
   test, and one real turn through the router.

## Part B — retire micro-mind, luxe-on-neo

### P4 — Design the luxe wiring (investigate before editing)
1. Read `configs/chat.yaml`, `src/luxe/chat/{launch,slots,modelcaps,origin}.py`,
   `chat/chat.sdd`, and CLAUDE.md's single-champion + fleet-sibling
   sections. The task: neo's `luxe chat`/`luxe code`/`luxe ready`/
   `luxe smoke` run against `http://127.0.0.1:8080/v1` (llama-server,
   OpenAI-compatible, no API key) by default ON NEO, while every other
   host's behavior stays byte-identical.
2. Design constraints:
   - This extends the SANCTIONED per-host manifest carve-out; the bench
     path (`single_64gb.yaml`, oMLX, the 35B champion) is untouched.
   - oMLX-specific surfaces must degrade loudly-but-gracefully against
     llama-server on neo: `origin` glyphs → unknown (no errors),
     `modelcaps` must NOT withhold tools from the 4B (its template has
     tool support — verify, don't assume), `/doctor`//`luxe ready` checks
     that are oMLX-only should skip/annotate rather than FAIL, `luxe pull`
     should refuse cleanly on a llama-server backend.
   - Whether this is a `hosts:` entry, a `backends:` default, or a small
     amount of new per-host config is the executor's design call — but
     keep it minimal, follow chat.sdd, and document the choice in the
     report. Do NOT redesign the manifest system for one host.
   - neo's engine identity (model name as served, port, ctx) already
     lives in dotfiles `luxe/neo-models.ini` — stay consistent with it.
3. Write the design as a short section in the report BEFORE implementing.

### P5 — Implement + test
1. Branch `feat/neo-unification` in this checkout. Code + config + tests
   (`tests/` per repo conventions: new behavior lands with tests).
2. Docs in the same commits: CLAUDE.md fleet-sibling section (neo no
   longer runs micro-mind; luxe-over-llama-server is the deployment;
   smoke-on-neo pin changes), chat.sdd (the "bare `luxe smoke` failing on
   neo is pinned" line changes to the new contract), lessons.md entry for
   the LaunchAgent gap + fix.
3. Full test suite on m5. Then deploy the BRANCH to neo's luxe checkout
   (push branch to origin and fetch/checkout on neo, or fetch direct from
   m5 — executor's call; do not touch neo's `main`), `uv sync` per the
   canonical host-sync recipe, and run G-LUXE-LOCAL's drills for real.
4. Re-run the drills once more after a router restart (cold engine) to
   catch warm-cache flattery.

### P6 — Retire micro-mind (neo is the living checkout)
1. Mirror the neo-llm-bench precedent: README banner + CLAUDE.md status
   ("superseded for deployment by luxe-on-neo, 2026-08-13; llama-server
   runtime + router config remain in service as neo's engine; harness,
   REPL, and bench fixtures are the canonical historical record for
   small-model guard design"), pointer to the luxe report. lessons.md
   stays — it is the valuable part.
2. Do NOT delete anything; do not touch the llama-server build, the
   router LaunchAgent/Daemon, or `neo-models.ini`.
3. Commit on neo's micro-mind checkout; do NOT push (review first).
4. Anything on neo that auto-invokes the micro-mind BINARY (cron,
   LaunchAgents, shell aliases, dotfiles wrappers): inventory it, disable
   or repoint to luxe equivalents, list every change in the report.

### P7 — Report + handoff to review
`acceptance/luxe_neo_unification_2026_08/REPORT.md` in this checkout:
gates table with pasted evidence, the P4 design section, every file/commit
touched on all three surfaces (luxe branch, neo micro-mind checkout, neo
system/dotfiles), deferred items, and a "reviewer checklist" section
telling Fable exactly what to re-verify independently (commands + expected
outputs). Nothing merged, nothing pushed to any `main`.

## Constraints
- m1/m4 untouched; m5 gets only the luxe branch + test runs. No model
  servers on m1/m4/m5 restarted.
- Availability outranks the project: if at any point neo's router can't
  be brought back healthy, stop and report immediately.
- Reboot only if P1.1 says it's safe for a headless box.
- Timebox ~4 h active work; blocked items go in the report as blocked,
  not half-done.
