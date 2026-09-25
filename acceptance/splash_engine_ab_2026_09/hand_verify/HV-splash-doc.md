# Hand-verify: splash4 (Qwen3.6-35B-A3B-Splash) — DOCUMENT/MANAGE fixtures, reps 1-3

Host: m5. All 3 maintain_rep{1,2,3} dirs had summary.json already (no polling needed).
Read-only throughout: no fixture repos, branches, or bench outputs were modified.

## Verdicts

| Fixture | Rep | Verdict | Reason | Diff stat |
|---|---|---|---|---|
| isomer-document-quickstart | 1 | REAL | README Quick Start clarified w/ docker-compose + port 27001; both verified against app.py:1138 (`port=27001`) and docker-compose.yml:13 (`127.0.0.1:27001:27001`). ISOMER_SECRET step preserved. | README.md +7/-3 |
| isomer-document-quickstart | 2 | REAL | Same shape, near-identical content (deterministic). | README.md +4/-3 |
| isomer-document-quickstart | 3 | REAL | Same shape. | README.md +4/-3 |
| lpe-rope-calc-document-typing | 1 | REAL | Added `f: io.BufferedIOBase` to `_read_gguf_value`, the exact single untyped top-level param the fixture note identifies (verified: all 13 other top-level defs already typed at base_sha). No redundant docstring added (correctly left existing one alone). | pe_scan.py +2/-1 (+ stray `__pycache__/*.pyc` binary, see note) |
| lpe-rope-calc-document-typing | 2 | REAL | Identical fix. | same |
| lpe-rope-calc-document-typing | 3 | REAL | Identical fix. | same |
| neon-rain-document-modules | 1 | REAL | "Subdirectories" section lists all 9 required dirs (actions/ai/data/debug/input/phases/render/state/ui — all verified present in the tree at base_sha) with accurate per-dir descriptions; correctly cites `ActionResolver` (verified class exists in src/actions/ActionResolver.js) and `EventBus` (verified src/EventBus.js exists) multiple times. Preserves rest of file (append-only diff). | ARCHITECTURE.md +12 |
| neon-rain-document-modules | 2 | REAL | Byte-for-byte same stat. | ARCHITECTURE.md +12 |
| neon-rain-document-modules | 3 | REAL | Same. | ARCHITECTURE.md +12 |
| the-game-document-architecture | 1 | REAL | "How It Works" section correctly names `src/App.jsx`'s `shuffle` function (verified: `async function shuffle()` at line 26, calls `/api/shuffle`) and `server.js`'s `selectWithNeighbors` (verified: exists at line 103, called line 135). Accurately describes the two randomization paths (Plex library window-pick vs. studio uniform-random). | README.md +10 |
| the-game-document-architecture | 2 | REAL | Same. | README.md +10 |
| the-game-document-architecture | 3 | REAL | Same. | README.md +10 |
| nothing-ever-happens-document-config | 1 | REAL | Comprehensive CONFIG.md, ~35 env vars across 10 sections w/ file:line citations. Spot-checked against bot/config.py and bot/main.py at base_sha: BOT_MODE/LIVE_TRADING_ENABLED/DRY_RUN/CONFIG_PATH defaults (bot/config.py:38-45) match exactly; PAPER_STARTING_BALANCE default "100" (bot/main.py:52) matches; DASHBOARD_HOST/PORT/DASHBOARD_TOKEN (bot/main.py:193-205) match. | CONFIG.md +127 (new file) |
| nothing-ever-happens-document-config | 2 | REAL | Same shape (125 lines vs 127, trivial wording variance). | CONFIG.md +125 |
| nothing-ever-happens-document-config | 3 | REAL | Same. | CONFIG.md +125 |
| nothing-ever-happens-manage-deps-audit | 1 | REAL | SECURITY-AUDIT.md documents exactly 3 real findings (aiohttp, web3, python-dotenv) against requirements.txt's actual 7 pinned deps (verified byte match). GHSA-5hr4-253g-cpx2 (web3 SSRF, fix 7.15.0) and GHSA-mf9w-mj56-hr94 (python-dotenv symlink, CVE-2026-28684, fix 1.2.2) both verified real via web search — not fabricated, and recommended fix versions match upstream advisories exactly. requirements.txt untouched as required. | SECURITY-AUDIT.md +98 (new file) — **see branch note** |
| nothing-ever-happens-manage-deps-audit | 2 | REAL | Same 3 findings, reworded (deterministic content, non-deterministic phrasing), same 98-line diff size. | SECURITY-AUDIT.md +98 (new file) |
| nothing-ever-happens-manage-deps-audit | 3 | REAL | Same. | SECURITY-AUDIT.md +98 (new file) |

**Tally: REAL 18 / THIN 0 / VACUOUS 0 / DAMAGING 0**

## Notable observations

1. **Branch-name collision, not overwrite, on deps-audit.** All 3 reps of `nothing-ever-happens-manage-deps-audit` planned branch `-19` but pr_state.json logged `commit failed: "no diff produced (failed_no_mutations_produced)"` for all three — yet grade.py's `git diff base_sha HEAD` legitimately found the 98-line diff each time. Root cause: the model's bash tool (git is allowlisted) self-committed `SECURITY-AUDIT.md` directly, so by the time `pr.py:_do_commit` ran its own `git status --porcelain` check, the tree was already clean and it misreported "no mutations." Branch `-19` was **never created** (confirmed absent, locally and on the fixture-cache origin) — this is a harness bookkeeping bug, not a grading defect. The 3 reps' actual commits landed on whatever branch was checked out at the time and were overwritten by the next fixture's `git reset --hard` before I could inspect them via git; verification instead relied on the immutable per-run `synthesizer.md` + `result.json`/`diagnostics.json` (safe, per-run_id, never touched by later resets).
2. **The other 5 fixtures' branches DID push correctly** (commit→push succeeded, only `gh pr create` failed for lack of a GitHub remote — same known, harmless limitation across the whole suite). Their local branch refs had since been pruned by the retention housekeeping (`_prune_old_branches`, keep=25/slug — heavy branch churn from other/older bench sessions sharing the same clones), but `remotes/origin/<branch>` in each clone still holds the exact per-rep pushed commit, which is what all git-diff-based verdicts above are drawn from.
3. **Determinism holds tightly** (temp=0, per luxe.sdd): isomer/lpe-rope/neon-rain/the-game diffs are byte-identical or near-identical in size across all 3 reps; deps-audit and doc-config vary only in prose phrasing while landing on the same findings/line counts.
4. **Minor hygiene defect (not scored):** lpe-rope-calc-document-typing's diff includes a stray `__pycache__/pe_scan.cpython-314.pyc` binary in every rep — the agent ran the script via bash and its own `git add -A`/`git commit` swept up the bytecode cache. Doesn't affect the goal or grading but is repo pollution the harness doesn't currently guard against for `document` tasks.
5. Tool-call counts scaled sensibly with task size (7-8 calls for the small README edits, 37-43 for the ~23-env-var CONFIG.md audit) and `aborted=False` on all 18 — consistent with genuine exploration, not shortcut/vacuous output.
6. All 18 result.json rows: `score=4/5`, losing only the `pr_opened` point to the known `gh auth`-less sandbox — this is uniform across the suite and not a fixture-specific signal.
