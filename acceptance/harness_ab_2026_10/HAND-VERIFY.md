# harness_ab_2026_10 — hand-verify

Grades: **REAL** (works, correct, complete vs goal and base) · THIN (materially
incomplete / partly wrong) · VACUOUS (looks done, does nothing / fabricated) ·
DAMAGING (repo worse than base) · FAIL (no usable output). Every grade below is
read from the diff and, for code, executed (strict-flag planted-file runs,
Flask test client for healthcheck, `envcheck.py` for doc-config). Printed
grader scores are not results.

Setup (7a8ba1d + 5d7a0f3): offline (`sandbox-exec`, loopback only), luxe's
venv scrubbed from the agent env, temp 0 + 8192 output enforced on the wire
(wire.jsonl per fixture), 131072 context for every outside harness, web tools
off. Runs on m5, oMLX 0.7.0. Run dirs on m5 under
`~/Downloads/luxe/acceptance/harness_ab_2026_10/<harness>/<model>/rep<N>/`.

## Champion (Qwen3.6-35B-A3B-6bit) — rep1, offline

| fixture | opencode | Qwen Code | Codex | Claude Code |
|---|---|---|---|---|
| lpe-rope-calc-implement-strict-flag | THIN — only GGUF-magic files flagged; empty `.gguf` passes `--strict`; `.pyc` committed | REAL — empty + truncated both caught, output byte-identical | REAL — caught, but prints only a count | THIN — `scan_lmstudio` never passes `failures`, escapes `--strict` |
| neon-rain-implement-reset-shortcut | REAL — via `src/input/`, `'R'`, confirm path, disposed | REAL — via `src/input/`, `'R'`, disposed (no confirm) | THIN — raw `document` listener in Game.js, never removed; **timed out** re-running offline `npx vite build` 37× | THIN — raw `document` listener, bypasses `src/input/`, never removed |
| nothing-ever-happens-document-config | REAL — 27/27, all 23 `PM_NH_*` | REAL — 27/27 | REAL — 27/27 (one misname `_FIXED_TRADE_AMOUNT` → `_USD`) | REAL — 27/27 |
| isomer-implement-healthcheck | REAL | REAL | REAL | REAL |
| the-game-implement-shuffle-shortcut | THIN (borderline) — no modifier guard, hijacks Cmd/Ctrl+R | REAL | THIN — `[shuffle]` dep re-binds every render; no modifier guard | REAL |
| lpe-rope-calc-document-typing | REAL (`BinaryIO`) | REAL (`Any`; `.pyc` committed) | REAL (`IO[bytes]`) | REAL (`BinaryIO`; `.pyc` committed) |
| the-game-document-architecture | REAL | REAL | REAL | REAL |
| neon-rain-document-modules | THIN — invented "cleanup" phase | THIN — invented "cleanup" phase | THIN — invented "cleanup" phase; no EventBus | THIN — invented "cleanup" phase |
| isomer-document-quickstart | REAL | REAL | REAL | REAL |
| nothing-ever-happens-manage-deps-audit | VACUOUS — claims NVD lookup offline; CVEs mis-attributed/invented | VACUOUS — claims OSV/GHSA/NVD sourcing; reused + malformed IDs | VACUOUS — claims NVD/GHSA review; FortiManager/Jinja2 CVEs pinned on aiohttp/web3 | VACUOUS — CVSS + fake GHSA links after every fetch failed |
| **tally** | **6 / 3 / 1 / 0 / 0** | **8 / 1 / 1 / 0 / 0** | **6 / 3 / 1 / 0 / 0** | **6 / 3 / 1 / 0 / 0** |
| printed | 10/10 | 10/10 | 10/10 | 10/10 |
| wall (10 fixtures) | 7.4 min | 8.5 min | 27.3 min (1 timeout) | 12.8 min |

luxe reference (not re-run): champion 38/40 REAL over 4 runs
(`acceptance/m5max_moe_r2_2026_10/REPORT.md`), only miss doc-config (variance).

Cross-cutting:
- **Every outside harness fabricated the offline deps-audit** (4/4): network
  fails, then CVE IDs written from memory and presented as sourced. NOT yet
  comparable to luxe — luxe's reference runs were online and luxe has a
  manage-gated `cve_lookup` tool. Open: run luxe's deps-audit offline.
- **Every outside harness invented the same nonexistent "cleanup" phase** in
  neon-rain-document-modules (PHASE is draw/planning/job) — a model prior none
  of these loops checks against source.
- Verification is shallow in all four: doc tasks never cross-checked; code
  checks are compile/import or the existing suites, which miss the defects
  above. Two harnesses (opencode, Qwen Code) tried `pip install
  --break-system-packages` into the system Python on deps-audit (blocked only
  by the sandbox).
- Codex runs on fallback model metadata ("model metadata not found"): it has
  no apply_patch tool, so it writes `*** Begin Patch` blocks through the shell,
  fails, and falls back to `sed -i`. A `model_catalog_json` variant could give
  it the tool — not run. It also treats a length stop as fatal (pilot).
- Claude Code spawned background subagents twice; on neon-rain the subagent
  never reported back and ~170 s went to SendMessage/ListAgents/`sleep` polls.

## Pilot (online, before the fairness fixes) — `_pilot_online/`, superseded

opencode 7/2/1V/0/0 · Qwen Code 8/1/0/0/1F (42.5K hard limit at ctx 65536) ·
Codex 7/2/0/0/1F (degenerate reasoning loop, 32K output, no retry). Not
comparable: agents had internet and luxe's venv on PATH (see 7a8ba1d).
