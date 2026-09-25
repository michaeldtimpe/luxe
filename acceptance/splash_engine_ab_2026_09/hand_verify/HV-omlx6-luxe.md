# Hand-verify: omlx6 arm (Qwen3.6-35B-A3B-6bit, oMLX), maintain_suite, 10 fixtures x 3 reps

Host: m5. Read-only throughout (no repos/branches/outputs modified, no processes touched).
Method: same as HV-omlx-luxe.md / HV-splash-*.md — diffs read via
`git -C <clone> diff <base_sha> origin/<branch>` (branches confirmed pushed today,
`git log -1 --format=%ci origin/<branch>` timestamps match the rep windows
16:57-17:22 local); `nothing-ever-happens-manage-deps-audit` graded via
`synthesizer.md` + `result.json` (branch bookkeeping bug, see below).

| Fixture | rep1 | rep2 | rep3 |
|---|---|---|---|
| isomer-document-quickstart | REAL — docker-compose+port 27001 ref (verified app.py:1138 `port=27001`, docker-compose.yml `127.0.0.1:27001:27001`), ISOMER_SECRET preserved (README.md +5/-3) | REAL — restructured into numbered steps, same facts, still non-destructive (README.md +25/-12) | REAL — same shape as rep1 (README.md +6/-3) |
| isomer-implement-healthcheck | REAL — `@app.route("/health")` on existing `app` instance, `jsonify({"status":"ok"}),200` (app.py +6) | REAL — byte-identical | REAL — byte-identical |
| lpe-rope-calc-document-typing | REAL — typed the one actual untyped param `f: BinaryIO` on `_read_gguf_value` (matches fixture's known 1-gap state), no redundant docstring added (pe_scan.py +2/-2) | REAL — byte-identical | REAL — byte-identical |
| lpe-rope-calc-implement-strict-flag | REAL — `--strict` threads `bad_gguf_count`/`bad` through `scan_ollama`/`scan_gguf_dir`/`scan_lmstudio`/`main()`, exits 1 only when strict+bad-GGUF found, default path unchanged (pe_scan.py +36/-13) | REAL — source byte-identical (only a stray committed `.pyc` differs, same hygiene defect as other arms) | REAL — byte-identical |
| neon-rain-document-modules | REAL — all 9 subdirs + EventBus; fact-checked: `EventBus.js` pub/sub confirmed, `ActionResolver.js` "mutates state" confirmed, `DebugLayout.js` Ctrl+Shift+D toggle confirmed at src/debug/DebugLayout.js:37 (ARCHITECTURE.md +20) | REAL — byte-identical | REAL — byte-identical |
| neon-rain-implement-reset-shortcut | REAL — wires `keydown` in the **existing** `src/input/HtmlInputHandler.js`, checks `e.shiftKey && !e.ctrlKey && !e.altKey && e.key === 'R'` (uppercase, correct per Shift+R DOM behavior), emits `game:restart` via EventBus consumed by `Game.js`, adds `dispose()` cleanup (Game.js +1, HtmlInputHandler.js +11) | REAL — byte-identical | REAL — byte-identical |
| nothing-ever-happens-document-config | REAL — 60 unique UPPER_SNAKE tokens, well-organized (Deployment/Secrets/PM_NH_* tuning/Dashboard/Risk sections), 3 claims fact-checked against bot/config.py (BOT_MODE:38, LIVE_TRADING_ENABLED:39, PRIVATE_KEY:117) — all correct (CONFIG.md +131) | REAL — 63 unique tokens, same shape, comprehensive (CONFIG.md +125) | **THIN** — only 26 unique tokens; omits the entire `PM_NH_*` strategy-tuning group (~20 vars, the largest category — market_refresh/price_poll/cash_pct/entry_price/slippage/retry/backoff/etc.); claims `DASHBOARD_PORT` default `8080` at bot/main.py:193, but the actual code (`dashboard_port = os.getenv("PORT") or os.getenv("DASHBOARD_PORT")`) has **no env default** — 8080 only exists as a dead `DashboardServer.__init__` param default never reached on that path (CONFIG.md +76) |
| nothing-ever-happens-manage-deps-audit | REAL* — no branch (known bookkeeping bug, see below); synthesizer.md shows 3 concrete correctly-sourced findings (GHSA-mf9w-mj56-hr94/CVE-2026-28684 python-dotenv, GHSA-5hr4-253g-cpx2/CVE-2026-40072 web3, GHSA-cq5v-8q36-5273/CVE-2026-69244 aiohttp) | REAL* — same 3 findings (aiohttp finding cites 2 extra GHSA ids as supplementary evidence, still 3 concrete package-level findings) | REAL* — same 3 findings, consistent |
| the-game-document-architecture | REAL — line-accurate refs, fact-checked: `selectWithNeighbors` at server.js:103 (confirmed), studio-pick `Math.random()` near server.js:141 (confirmed), `shuffle()` in App.jsx:26 (confirmed, function starts line 27 — one-line-off but same location) (README.md +22) | REAL — byte-identical | REAL — byte-identical |
| the-game-implement-shuffle-shortcut | REAL — binds `keydown` once via `useEffect`, accepts `'r'`/`'R'` (goal only asked for lowercase 'r', no Shift), skips INPUT/TEXTAREA/contentEditable, calls existing `shuffle()` (App.jsx +16) | REAL — byte-identical | REAL — byte-identical |

\* `nothing-ever-happens-manage-deps-audit`: branch `luxe/manage/audit-requirements-txt-identify-any-pinned-19` does not exist on origin in any of the 3 reps (confirmed: `git log -1 origin/<branch>` fails "unknown revision") — matches the known bookkeeping bug (model self-commits via allowlisted git, harness's own commit step then sees a clean tree and misreports `failed_no_mutations_produced`). Graded via `~/.luxe/runs/<id>/synthesizer.md` per the established workaround; not a model failure.

## Tally (30 cells)
REAL: 29 (incl. 3 REAL\*) | THIN: 1 | VACUOUS: 0 | DAMAGING: 0

## Per-rep summary.json
| rep | passed | score | avg_wall_s | avg_tokens |
|---|---|---|---|---|
| 1 | 10/10 | 40/50 | 64.4 | 152480 |
| 2 | 10/10 | 40/50 | 41.6 | 121159 |
| 3 | 10/10 | 40/50 | 41.3 | 137208 |

All 30 printed fixture-level scores were 4/5 (the uniform `pr_opened=0` deduction —
`gh pr create` has no GitHub remote in this sandbox — same env limitation noted in
every other arm's hand-verify, not a fixture- or model-specific signal).

## Observations
- The 6-bit champion is the most deterministic arm hand-verified so far: 8 of 10
  fixtures produced byte-identical diffs across all 3 reps (only
  `isomer-document-quickstart` and `nothing-ever-happens-document-config` vary
  in prose/structure/coverage).
- `neon-rain-implement-reset-shortcut` correctly uses uppercase `e.key === 'R'`
  in all 3 reps — this arm does NOT reproduce the lowercase-`'r'` footgun that
  made one omlx4 rep THIN on this exact fixture.
- `nothing-ever-happens-document-config` rep3's coverage collapse (60/63 → 26
  unique tokens) is the same failure shape flagged as the highest-risk fixture
  in HV-omlx-luxe.md: an open-ended "every env var" task where the loose
  `min_matches=15` grader still passes on partial coverage.
- `the-game-implement-shuffle-shortcut`'s `test_passed=False` in diagnostics is
  confirmed NOT model-caused (no `test` script in package.json at base_sha,
  same as every other arm).
- rep1's higher `avg_wall_s` (64.4 vs ~41 for reps 2-3) is a cold-start/load
  artifact, not a content-quality difference — the diffs it produced are
  identical or equivalent in substance to reps 2-3.
