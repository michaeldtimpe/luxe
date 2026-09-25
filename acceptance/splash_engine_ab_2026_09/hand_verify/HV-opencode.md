# Hand-verify: opencode agent harness, m5, 2026-09-24

## 1. Scores (all 40 cells: outcome_points 3/3, score 4/4, timed_out=false, exit_code=0)

| fixture | omlx r1 (wall/calls) | omlx r2 (wall/calls) | splash r1 (wall/calls) | splash r2 (wall/calls) |
|---|---|---|---|---|
| lpe-rope-document-typing | 34.2s/7 | 25.8s/7 | 12.5s/5 | 8.0s/5 |
| lpe-rope-implement-strict-flag | 61.7s/13 | 52.9s/12 | 31.5s/13 | 25.7s/13 |
| the-game-implement-shuffle | 32.3s/7 | 26.2s/7 | 18.3s/6 | 20.1s/6 |
| the-game-document-architecture | 20.1s/5 | 17.0s/5 | 14.5s/5 | 12.1s/5 |
| neon-rain-implement-reset | 54.0s/14 | 31.8s/11 | 27.8s/16 | 22.7s/16 |
| neon-rain-document-modules | 33.2s/14 | 24.2s/14 | 13.0s/4 | 11.3s/4 |
| isomer-document-quickstart | 31.4s/5 | 24.2s/5 | 25.2s/6 | 17.0s/6 |
| isomer-implement-healthcheck | 26.0s/4 | 16.7s/4 | 15.9s/4 | 10.4s/4 |
| nothing-ever-happens-manage-deps-audit | 134.7s/46 | 110.6s/25 | 77.2s/18 | 55.3s/18 |
| nothing-ever-happens-document-config | 126.5s/17 | 83.9s/14 | 37.1s/9 | 26.0s/9 |
| **totals** | 553.9s | 413.2s | 273.0s | 208.5s |

All-10-pass on every run for both engines; grader max is 4/5 (no PR ever opened) so this table doesn't distinguish quality — see verdicts below. splash4 rep2 vs rep1 is byte-identical on 9/10 diffs (near-zero input tokens = cache hit); omlx varies rep-to-rep on most fixtures.

## 2. Verdicts (25 unique diffs cover the 40 cells; identical diffs share one verdict)

| fixture | omlx r1 | omlx r2 | splash r1=r2 |
|---|---|---|---|
| lpe-rope-document-typing | REAL (shared, all 4 identical) | = | = |
| lpe-rope-implement-strict-flag | REAL but junk `__pycache__/*.pyc` committed | **DAMAGING**: silently drops `scan_lmstudio`'s MLX/HF-dir detection, replaces with wrong `scan_hf_cache` call — regresses default (non-strict) behavior; junk `.pyc` committed | REAL: correctly inlines `scan_lmstudio`'s logic instead (functionally equivalent); junk `.pyc` committed |
| the-game-implement-shuffle | REAL | REAL | REAL (merges into existing effect, still bound once; also fires on Shift+r, extra but harmless) |
| the-game-document-architecture | REAL (shared) | = | REAL (shared) — both say pick is "centered" in window, which is an oversimplification (position is randomized within the 6-item window, not always centered) |
| neon-rain-implement-reset | **THIN**: `e.key === 'r'` after `e.shiftKey` — never fires on a real Shift+R press (key becomes uppercase `R`); same bug as splash | REAL: only variant using `e.key === 'R'`, matches the file's own existing convention (`DebugLayout.js` uses `e.ctrlKey && e.shiftKey && e.key === 'D'`) | **THIN**: same lowercase-`r` bug as omlx r1 — shortcut never fires |
| neon-rain-document-modules | REAL | REAL | REAL — EventBus not re-mentioned in the new table, but it's already covered extensively in preserved existing content, so goal's clause is satisfied |
| isomer-document-quickstart | REAL, port/compose claims verified against app.py/docker-compose.yml | = | REAL, adds a dashboard-behavior claim also verified against the `/` route |
| isomer-implement-healthcheck | REAL (shared, all 4 identical) | = | = |
| nothing-ever-happens-manage-deps-audit | **THIN**: really ran `pip-audit -r requirements.txt` (real tool, exit 0, "No known vulnerabilities found") but reports 0 CVEs against a goal that wants "≥3 concrete findings"; satisfies the letter only via version-range rows, not real advisories | REAL, mostly grounded: 3/4 CVEs (aiohttp×3, sqlalchemy/snowflake) confirmed by actually fetching the real GHSA advisory pages (titles match exactly); 1 finding (web3 SSRF CVE-2026-40072) asserted with full CVSS/detail but was **never fetched** — unverified | **Confabulated**: report cites 4 CVEs with CVSS scores + detailed technical descriptions, but every successful webfetch call in its own trace returned only nav chrome / search-result shells (no advisory content ever loaded) — the specific CVE numbers/severities are not backed by its own tool output |
| nothing-ever-happens-document-config | REAL, but fabricates `DASHBOARD_PORT` default as `8080` — source has **no default** (falls back to `None`/dashboard skipped; docker-compose sets 9090–9106 per bot, never 8080) | Same `8080` fabrication, reproduced in both omlx reps | REAL — correctly reports `DASHBOARD_PORT` as `*(none)*`; `BOT_MODE=paper` verified correct in all 3 variants |

## 3. Tally per engine (10 fixtures × 2 reps = 20 cells each)

- **omlx4**: REAL 14, THIN 5 (implement-reset r1, manage-deps-audit r1+r2, document-config r1+r2), DAMAGING 1 (implement-strict-flag r2). Junk `.pyc` committed in both strict-flag reps.
- **splash4**: REAL 16, THIN 4 (implement-reset r1+r2, manage-deps-audit r1+r2 — the confabulated-CVE case). Junk `.pyc` committed in both strict-flag reps. Zero DAMAGING.
- Grader (outcome_points) shows 30/30 for all 4 runs — none of the above (bug, regression, fabrication) was caught by the automated grader.

## 4. Observations (behavior, ≤12 lines)

- No run in either engine executed a test suite or linter; code fixtures used `read`/`edit`/occasional `bash ls` only — verification is entirely the harness grader, not the agent.
- `webfetch` is the dominant/riskiest tool: manage-deps-audit made 14–37 calls/run with 4–26 errors; omlx r1's `pip-audit`/`webfetch` mix under-reported real CVEs, omlx r2 got 3/4 CVEs genuinely from GHSA, splash got 0/4 genuinely grounded despite reporting 4 with full detail — a confident-fabrication pattern, not a capability gap (splash *tried* to fetch, just never got past nav-chrome pages and reported anyway).
- The Shift+R key-case bug (`e.key==='r'` instead of `'R'`) is reproducible: omlx r1 and both splash reps all made it independently; only omlx r2 got it right, and only by reusing the codebase's own established `DebugLayout.js` convention.
- omlx r2's `lpe-rope-calc-implement-strict-flag` DAMAGING regression (dropped `scan_lmstudio`'s MLX/HF-dir scan, substituted the wrong function) is unrelated to the requested feature — a silent side effect of a mechanical refactor while wiring the `--strict` flag through call sites.
- Every rep of `lpe-rope-calc-implement-strict-flag` (4/4, both engines) committed a stray `__pycache__/pe_scan.cpython-314.pyc` binary — the fixture repo has no `.gitignore`, and opencode's own test-execution of `pe_scan.py` leaked into the diff.
- splash4 is consistently faster (208–273s vs omlx's 413–554s total) and near-deterministic rep-to-rep (9/10 fixtures byte-identical diffs, confirmed by cache-hit near-zero input tokens) at temperature 0; omlx is not deterministic across reps despite same temperature/model.
- Tokens: splash4 uses a `reasoning` field (300–3000 tokens/fixture) that omlx never populates — consistent with Splash's separate reasoning channel; heaviest fixture (manage-deps-audit) burns most output+reasoning tokens on both engines (webfetch loop).
