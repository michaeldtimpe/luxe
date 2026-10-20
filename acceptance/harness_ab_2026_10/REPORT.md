# harness_ab_2026_10 — outside harnesses vs luxe

User request (2026-10-18): is luxe's loop overfit to Qwen3.6, and should a
mainstream agent harness be the fallback loop instead? Five harnesses ×
three models × the 10 maintain_suite fixtures × 3 reps, offline, every pass
hand-verified. Per-run grades and evidence: `HAND-VERIFY.md`. Run log:
`NOTES.md`. Run dirs on m5 under this directory.

## Setup

- Harnesses: **opencode** 1.19.3, **Qwen Code**, **Codex** (codex-cli
  0.163.0, fallback model metadata, no apply_patch tool), **Claude Code**,
  and **codex-cat** (Codex + a `model_catalog_json` entry, so apply_patch
  reaches the model through the shim's custom→function tool translation).
- Models: **Qwen3.6-35B-A3B-6bit** (the champion), **Qwen3.8-27B-8bit**
  (dense), **Muse-Glimmer-30B-6bit**. All served by oMLX 0.7.0 on m5.
- Same conditions for every harness:
  - A wire shim forces temperature 0 and 8192 output tokens per request and
    logs every request (`wire.jsonl`).
  - Context is 131072 for every harness.
  - Web tools are off.
  - `sandbox-exec` allows loopback-only networking.
  - From rep2 onward, `~/.luxe/cve_cache` is denied (9c0de33).
  - luxe's venv is scrubbed from the agent's environment.
  - The cap is 3600 s per fixture.
- The luxe numbers are the existing hand-verified references, not re-runs:
  - champion and Glimmer: `m5max_moe_r2_2026_10`, 4 runs;
  - Qwen3.8: `tuning_2026_10` q38full, 3 reps.

  Those runs were online. Their deps-audit used luxe's `cve_lookup` against
  live OSV and graded REAL every time. An outside harness cannot do that
  offline, so the fair comparison is over **the nine other fixtures**. A
  separate offline luxe control covers deps-audit (below).

## Results: REAL over the nine non-audit fixtures

| model | opencode | Qwen Code | Codex | Claude Code | codex-cat | **luxe** |
|---|---|---|---|---|---|---|
| Qwen3.6-35B-A3B (champion) | 18/27 (67%) | 20/27 (74%) | 16/27 (59%) | 19/27 (70%) | 16/27 (59%) | **34/36 (94%)** |
| Qwen3.8-27B-8bit | **24/27 (89%)** | 22/27 (81%) | 23/27 (85%) | **24/27 (89%)** | 23/27 (85%) | 21/27 (78%) |
| Muse-Glimmer-30B-6bit | 19/27 (70%) | 19/27 (70%) | 20/27 (74%) | 20/27 (74%) | 19/27 (70%) | **30/36 (83%)** |

Over all ten fixtures (deps-audit included), the outside harnesses scored 0
REAL on deps-audit in all 45 runs. luxe's online runs scored REAL every time.

Average wall per 10-fixture cell, in minutes. luxe's figure is its own
reference:

| model | opencode | Qwen Code | Codex | Claude Code | codex-cat | luxe |
|---|---|---|---|---|---|---|
| champion | 9.0 | 9.5 | 13.7 | 14.2 | 21.3 | 8.1 |
| Qwen3.8 | 64.5 | 164.9 | 78.5 | 102.3 | 49.3 | ~73–97 |
| Glimmer | 54.0 | 63.7 | 81.4 | 68.3 | 83.2 | 47.5 |

## Is luxe overfit to Qwen3.6?

**Partly, yes, and it is a matter of fit, not breakage.**

- **Champion.** luxe is 20+ points ahead of every outside harness (94% vs
  59–74%). The outside harnesses' champion failures are verification
  failures:
  - three keyboard shortcuts that can never fire (wrong element, wrong
    event bus, inverted guard), all VACUOUS;
  - a return-type change that crashes the default path (DAMAGING ×2);
  - missing modifier guards;
  - cut-off turns that end the run.

  luxe's gates and prompts catch exactly this model's habits.
- **Qwen3.8.** Every outside harness beats luxe (81–89% vs 78%).
  - On the five code fixtures this model is REAL in 72 of 75 outside runs.
  - In luxe it loses doc-config (write-avoidance, 0/3) and healthcheck
    (`orphan_file` litter, 3/3). The cleanup turn added since fixes the
    litter: healthcheck 3/3 in `tuning_2026_10` §6.
  - The outside harnesses don't gate on litter, and they gave the model room
    to write the config doc.
  - luxe is not tuned for this model, and its gates cost it here.
- **Glimmer.** luxe still leads (83% vs 70–74%) despite neon-reset:
  - Glimmer is **0/4 on neon-reset inside luxe and 14/15 REAL under the
    outside harnesses**. In luxe it re-reads the same `Game.js` region
    until max steps (`tuning_2026_10` trace). That is a real luxe×Glimmer
    loop interaction.
  - Outside luxe, Glimmer's `from io import BinaryIO` prior ships an
    ImportError: DAMAGING ×4 across the cell (Codex r1, Qwen Code r2 and
    r3, Claude Code r3), all reproduced. `py_compile`, the check most agents
    use, cannot see it. Claude Code caught it once (r2), by running
    `--help`; in r3 it shipped it.
- **Harness choice barely matters within a model.** The spread across five
  harnesses is ≤4 REAL per model. The spread across models within one
  harness is up to 7. The model sets the ceiling. luxe adds the most for the
  model it was tuned on.

## Should a mainstream harness be the fallback loop?

**No — not as a replacement.** No outside harness matched luxe on the
champion, and the champion is the model the fallback kit actually runs. The
best outside cell with the champion (Qwen Code, 74%) is luxe minus 20 points.
On Qwen3.8, opencode (faster than luxe) and Claude Code (similar wall) are a
reasonable second loop.

What luxe has that none of the four do, from the evidence in this run:

1. **A cut-off turn is retried, not shipped as a completion.** At 8192
   output tokens the dense model regularly spends a whole turn reasoning:
   - opencode ends on `length` and exits 0;
   - Codex stops on `max_output_tokens`, sometimes with exit 0;
   - Qwen Code re-sends five identical temp-0 requests, then aborts.

   13 of Qwen3.8's 14 outside FAILs ended at a length or progress limit
   with nothing written (4 of them on non-audit fixtures). The champion had
   3 such empty diffs (opencode r2 neon, Codex r2 strict-flag, codex-cat r2
   quickstart). luxe's truncated-turn retry (default ON) exists for
   exactly this.
2. **Under an outage, it fails instead of fabricating.** See below.
3. **It reads the source before documenting.** neon-rain-document-modules
   was THIN in **45/45** outside runs. Every run copied the base repo's stale
   `ARCHITECTURE.md` (a nonexistent "cleanup" phase), and most never opened
   `src/`. luxe was REAL on it in every reference run for the champion and
   Qwen3.8.
4. **Verification depth.** Outside agents checked with `py_compile` /
   `node --check` / the existing suite. That is how the VACUOUS shortcuts and
   the DAMAGING imports printed as passes.

## deps-audit offline: fabrication vs failure

- **Outside harnesses, 45 runs: VACUOUS or FAIL, never REAL.** Every report
  that got written presents CVE/GHSA IDs from model memory as looked up.
  - Some IDs are real but filed under the wrong package (Jinja2 and urllib3
    CVEs under aiohttp and websockets).
  - Some are invented (CVE-2025-692xx batches).
  - Some are literal placeholders (`GHSA-xxxx-xxxx-xxxx`).
  - Several reports disclose the outage and still claim an NVD/GHSA method.

  Where Qwen3.8 FAILed, its reasoning was already fabricating when the length
  limit hit, so those are failures by accident, not honesty.
- **The luxe control** (champion, same sandbox, cache denied, 3 reps) gave
  FAIL/FAIL/FAIL: no file written, nothing fabricated. The cause:
  - `cve_lookup` errored;
  - the model retried the lookups;
  - luxe's stuck-loop guard aborted the run.

  This does not prove luxe would refuse to fabricate given more steps. It does
  show that under an outage luxe ends loudly, while every outside harness
  shipped a confident, falsely sourced audit that printed as a pass.

## apply_patch (codex-cat)

The Codex patch grammar depends on the model:

| model | apply_patch calls accepted |
|---|---|
| Qwen3.8 | 36/38 over 3 reps, no fallback, no junk; fastest Qwen3.8 cell |
| champion | 5 of ~177 over 3 reps (r1 2/~22, r2 1/140, r3 2/15) |
| Glimmer | r1 11/31, r2 ≥3 of ~53, r3 16/31 |

The champion and Glimmer write unified diffs inside the envelope. After
rejections they fall back to `sed -i ''`, heredocs, or Python `re.sub`. That
fallback produced:

- both codex-cat DAMAGING grades (a tuple return that crashes callers; a
  deleted `__main__` guard);
- the quickstart's literal `+` lines;
- a 124-rejection timeout.

Twice an "accepted" patch deleted a file (`ARCHITECTURE.md`, `app.py`). Both
times the agent restored it with git. Giving Codex its native edit tool
helped only the model that already speaks its grammar.

## Fairness and isolation caveats

- **The sandbox blocks the network, not reads.** Agents ran `find /`, listed
  `~/.cache/uv`, `~/.venvs`, `~/Downloads/*/.venv` and Chrome profile paths,
  grepped sibling clones in `~/.luxe/harness-ab-workspace`, used
  `~/luxe-bench-tmp` hand-verify venvs to run Flask, and used the real
  `~/.npm` cache.
  - Glimmer rep1's Claude Code subagent read three `cve_cache` entries (its
    report used none of them). From rep2 on, the cache was denied, with 0 hits.
  - No read changed a grade. But "could the agent verify" depends partly on
    what lives on the host.
- **Host `/tmp` is shared across runs.** One Claude Code self-test picked up
  another run's planted `.gguf` files and `rm -rf`ed the directory; no grade
  changed.
  - Left behind on m5: `/tmp/isomer-venv` and `/tmp/pe_test`. Not removed,
    because the verifiers were read-only.
  - Workspace clones are `git clean -fdx`ed between runs, so nothing carries
    over across runs.
- Qwen Code keeps memory files in the cell's sandbox HOME. Two fixtures on
  the same repo within a cell can share state. Nothing carries across cells.
- Three rep1 cells' walls were contended by a concurrent probe (`NOTES.md`).
- opencode, Qwen Code and Claude Code each tried
  `pip install --break-system-packages` on host Python during deps-audit.
  Only the network block stopped it.
- The printed grader is unreliable in both directions:
  - false passes: every VACUOUS and DAMAGING above;
  - false failures: a `destructive_diff` on a correct quickstart, and
    min-lines gates on correct docs.

## MCP arm: luxe's tools, not its loop

The champion was re-run under Qwen Code and Claude Code with luxe's tool
layer attached over MCP: `bm25_search`, `find_symbol`, ruff, mypy, bandit,
pip-audit, and `cve_lookup` on manage. Each tool appears exactly when luxe
would offer it. Conditions are otherwise identical, 3 reps.

| | REAL / 27 non-audit | without MCP | luxe |
|---|---|---|---|
| Qwen Code + luxe tools | 21 (78%) | 20 (74%) | 34/36 (94%) |
| Claude Code + luxe tools | 21 (78%) | 19 (70%) | |

**luxe's tools do not carry luxe's advantage; its loop does.**

- **The models barely used the tools.** Any luxe tool was called on 3/27
  (Qwen Code) and 9/27 (Claude Code) non-audit fixtures. Across 30 implement
  runs an analyzer checked the change once: ruff, on Claude Code r3
  strict-flag, after the edit, finding nothing new. Bandit never ran outside
  deps-audit.
- **The failures luxe avoids stayed put:**
  - stale-doc modules: THIN 6/6. `bm25_search` / `find_symbol` were never
    pointed at `src/`.
  - shuffle modifier-guard misses: 3/6.
  - neon-reset non-REAL: 3/6, including one shortcut that can never fire.
- **On deps-audit the tools failed offline exactly as they do inside luxe.**
  The difference is what happened next. luxe's loop aborted with nothing
  written. Both outside loops wrote the audit from memory and attributed it
  to OSV / published advisories, 6/6.
- **Harness quirk:** Qwen Code hides MCP tools behind a
  tool_search/tool_call bridge unless the server sets `alwaysLoadTools`.
  With the bridge, the champion reached a luxe tool on 1 fixture of 10.
  Anyone attaching luxe's MCP server to Qwen Code needs that setting.

So the plan from the fallback-harness discussion — make luxe's layers 1–2
harness-independent and let the loop be replaceable — holds for the *ops
kit*. But for this model, the measured value is in layer 3: the loop's
nudges, gates, retries and stop conditions. A mainstream loop with luxe's
tools attached is still a mainstream loop.

## Next

- Not requested, but cheap and worth doing: trace Glimmer neon-reset in luxe
  (0/4 in luxe, 14/15 outside). It is the one fixture where luxe's loop is
  clearly the problem.
- If luxe's tools are to be served to outside harnesses for real, the
  server needs instructions that make the model use them. A prompt-level
  nudge would be a new arm: unbenched, and a change to the neutral task
  text.
- Optional: on m5, `rm -rf /tmp/isomer-venv /tmp/pe_test /tmp/test_gguf
  /tmp/empty_gguf /tmp/audit-venv`. Bench agents left these behind.
