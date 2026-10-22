# Secret scrub, redaction and egress refusal — no-harm A/B (2026-10-22)

PR #47, main `d868bcf..20b39b2`. This started as a review of the video "Your Local AI Is Not as Private as You Think"
(Kai, 2026-10-21). Its threat model: a backdoored fine-tune, a poisoned chat template or a prompt injection steers an
agent into reading credentials and sending them out. Sandboxing was ruled out of scope by the user.

## What shipped

- **Env scrub.** `luxe.secrets.scrubbed_env()` drops secret-named variables. A name is secret if it is listed in
  `~/.luxe/secrets.env`, was resolved by luxe, or is credential-shaped (KEY/TOKEN/SECRET/PASSW/CREDENTIAL/PRIVATE/
  COOKIE, `_PAT`, `_AUTH`). It is a denylist, so JAVA_HOME and the like keep working. It applies to every subprocess
  the model drives: bench and chat bash, analyzers, test runners, gitkit verify and the smoke drill. It also applies
  to every git luxe runs for itself, because hooks, `diff.external` and filters execute code. `gh` is not scrubbed.
- **Redaction.** Known secret values in any tool result or error are replaced with `[REDACTED:<NAME>]` before the
  model sees them (`agents/loop.py::_redact_secrets`). This emits a `secret_redacted` event.
- **Egress refusal.** A tool call whose arguments carry a known value is not run. This covers raw, percent-encoded
  and base64 forms. The model gets a refusal that names the variable, never the value, and a
  `secret_egress_refused` event is emitted.
- **`.git` write guard.** `write_file` and `edit_file` refuse any path inside `.git`.
- **Chat warning.** Chat prints a one-line warning when web tools and bash are both on.
- **Not a sandbox.** Allowlisted bash includes `python`, so a model can read `~/.luxe/secrets.env` from disk and send
  it out in one command. tools.sdd and CLAUDE.md say so.

## The A/B caught a real bug

The first run (`dd6efd7`, log in `failed_first_attempt.log`) ended with **trt 0/30**: every run died at preflight
with "uncommitted changes".

- **Cause:** the bench pins `commit.gpgsign` through git's env-config protocol (`GIT_CONFIG_COUNT`,
  `GIT_CONFIG_KEY_0`, `GIT_CONFIG_VALUE_0`). `GIT_CONFIG_KEY_0` matched the KEY pattern and was dropped. git then
  failed with `unable to parse command-line config`, and `pr.is_dirty` reads a failed status as dirty.
- **Fix:** `20b39b2` keeps `GIT_CONFIG_KEY_<n>` (and adds `KEYRING` to the benign words), with a regression test
  that sets those variables.
- None of the 4,289 unit tests set `GIT_CONFIG_*`, which is why this only showed up in the bench.

## Result (second run; champion, 10 fixtures × 3 reps, interleaved; ctl = `d868bcf`, trt = `20b39b2`)

The launch script exported all of `secrets.env` into the environment on purpose, to test under the worst case.

| | ctl | trt |
|---|---|---|
| expected_outcome | **30/30** | **30/30** |
| score | 120 | 120 |
| byte-identical diffs | 9/10 fixtures identical in all 6 runs | |
| gates triggered / step-limit bailouts | 0 / 0 | 0 / 1 |
| `secret_redacted` / `secret_egress_refused` | — | **0 / 0** (no false positives in 30 runs) |

- **document-config** varied in both arms, as it always does. trt rep3 reproduces the known-good `27a0` doc.
- **Hand-verify:** all six docs have every `PM_RISK_*`, `BOT_MODE`, `DRY_RUN` and `PM_NH_MAX_ENTRY_PRICE` default
  right.
- **One model error in both arms:** a `DASHBOARD_PORT` default of 8080 that the source doesn't have, in ctl rep3 and
  trt rep1–2.
- **Verdict:** no harm.

Smoke after deploy (`20b39b2`): `luxe ready` and `luxe smoke --chat --code` READY on m1 (107 s), m5 (10 s) and neo
(43 s).
