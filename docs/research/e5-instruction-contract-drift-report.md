# E5 · Instruction / contract drift check (luxe)

Read-only cross-read of luxe's two instruction docs against the four `.sdd`
contracts. The question: *do they ever contradict, overlap, or silently
override each other?* No writes to luxe, no model loaded, no fixes applied —
this is the audit pass and the methodology for a future automated lint.

**Sources cross-read (line counts authoritative as of 2026-05-25):**

| Source | Lines | Role |
|---|---:|---|
| `CLAUDE.md` | 77 | Onboarding doc, auto-loaded by Claude Code |
| `AGENTS.md` | 286 | "Agent Reference" — see Finding D1 |
| `src/luxe/luxe.sdd` | 33 | Root invariants (Must / Must not / Owns / Forbids) |
| `src/luxe/tools/tools.sdd` | 20 | Tool-side enforcement order |
| `src/luxe/agents/agents.sdd` | 63 | Prompt registry + reflect + adaptive-policy invariants |
| `benchmarks/maintain_suite/maintain_suite.sdd` | 21 | Bench rules |

---

## 0. Findings & interpretation (TL;DR)

Eight drift items, ranked by severity. **One is critical**, two are medium,
five are low — and the critical one is structural, not a wording quibble.

### 🔴 D1 (critical) — `AGENTS.md` is stale top-to-bottom and structurally contradicts the live architecture

`AGENTS.md` opens with this banner (lines 3-7):

> **Note:** This document describes the swarm pipeline's per-stage agents
> and predates the v1.0 additions (single-mode monolith, validator's
> structured envelope contract, BM25/AST tools added to worker surfaces).
> See the README for the v1.0 surface and tool list.

…and then the next **279 lines** describe the *retired* swarm pipeline as if
live: an Architect → Worker (Read/Code/Analyze) → Validator → Synthesizer
chain, each with model assignments
(`Qwen2.5-7B-Instruct-4bit`, `DeepSeek-Coder-V2-Lite-Instruct-4bit`,
`Qwen2.5-Coder-14B-Instruct-MLX-4bit`, `Qwen2.5-32B-Instruct-4bit`), per-role
tool surfaces, escalation rules, etc. These directly contradict the live
contract on three fronts:

1. **`luxe.sdd` Forbids `src/swarm/**`, `src/micro/**`, `src/phased/**`.**
   The roles AGENTS.md describes live in those forbidden trees; the
   `tools/fs.py` write guards would actually *block* a model trying to
   re-create them (role-path fuzzy match, `_check_spec_forbids`).
2. **`CLAUDE.md` §"Single-champion policy" pins exactly one MoE model**
   (`Qwen3.6-35B-A3B-6bit`); AGENTS.md lists four other models as live.
3. **`agents.sdd` Must "`run_agent` (loop.py) owns the inner tool-call
   loop; `single.py` is the entry"** — AGENTS.md's loop description points
   at `src/swarm/agents/loop.py:run_agent` (with an inline redirect in the
   banner, but the body never updates the references).

**Why this is critical, not cosmetic:** Both human onboarders and any tool
that ingests `AGENTS.md` as a primary instruction surface (the *Codex/Claude
agent file* convention — the file's literal name advertises it as the agent
config) get a fictional architecture. The banner approach is fragile: a
reader who skims past the first 7 lines, or a retrieval tool that excerpts
the body, learns wrong-by-construction information. The 286-line body
substantially outweighs the 4-line banner in any attention-weighted reading.

**Compounding the issue:** `CLAUDE.md` §47-62 lists six "When working on
this repo" rules but does **not** redirect away from `AGENTS.md`. A new
onboarder reading the obvious "agent reference" file is not warned off it.

### 🟡 D2 (medium) — Single-champion model pin lives only in `CLAUDE.md`

The positive statement *"use `Qwen3.6-35B-A3B-6bit`"* exists only in:
- `CLAUDE.md:8` ("luxe pins exactly one MoE model: `Qwen3.6-35B-A3B-6bit`")
- `configs/single_64gb.yaml` (the runtime config)

`luxe.sdd` only states the **negative**: "Must not: Use MoE Instruct-2507
model family" and "Must not: Configure dense >30B mxfp8 models" — both
exclusions. There is no Must-rule pinning the positive champion. So if a
future commit changes `configs/single_64gb.yaml` to a different non-excluded
model, neither the `.sdd` chain nor any tool-side guard catches it —
`CLAUDE.md` is the lone normative source. Given P2 (safety outside the
prompt) is luxe's signature strength, leaving a load-bearing positive pin
in a single Markdown file is the odd one out.

### 🟡 D3 (medium) — v1.11 + reflect invariants live only in `agents.sdd`

`agents.sdd` carries dense, version-pinned invariants that are nowhere in
`CLAUDE.md`:
- Reflect stage (Track 1, lines 13-36): opt-in via `LUXE_REFLECT`,
  disable-equivalence requirement, `gap` substantiation rule,
  anti-overfitting prompt constraints.
- Stage 3 / v1.11 adaptive policy (lines 38-51): score_log ownership,
  bias-not-lock, slew-rate limit (`LUXE_ADAPTIVE_MAX_INTENSITY_DELTA_PER_STEP`),
  `LUXE_ADAPTIVE_POLICY=0` disable-equivalence, log-only priors.

Both subsystems are opt-in / default-off, so this isn't a contradiction —
it's a discoverability gap. A user toggling `LUXE_REFLECT=1` or
`LUXE_ADAPTIVE_POLICY=1` from `CLAUDE.md`'s guidance alone never sees the
invariants that protect those modes. Worth a one-line pointer in `CLAUDE.md`.

### 🟢 D4 (low) — Prompt-registry rule stated 3×, will drift if any one is edited

"All mono prompt strings live in `src/luxe/agents/prompts.py`" appears in:
- `CLAUDE.md:52-54` ("Prompts go through `src/luxe/agents/prompts.py`…")
- `luxe.sdd:14` ("All mono prompt edits go through `src/luxe/agents/prompts.py` registry")
- `agents.sdd:7` ("All mono prompt strings live in `src/luxe/agents/prompts.py` `PROMPT_REGISTRY`")
- (And the negative restatement `agents.sdd:54` "Inline prompt strings in `single.py` or `cli.py`")

All four currently agree. Triple duplication is the failure mode: edit one
(say, rename the file or move the registry to a submodule) and the others
silently lag.

### 🟢 D5 (low) — Enforcement *order* in `tools.sdd` not summarized anywhere else

`tools.sdd:7-8` specifies the precise order: honesty guards (cheap)
*before* `_check_spec_forbids` (expensive), and `SddParseError` catch
*before* `ValueError` catch (because the former subclasses the latter).
`CLAUDE.md:42` only labels the file ("honesty guards + Forbids enforcement
order") without restating the order itself. A reader who only opens
`CLAUDE.md` gets the bookmark but not the rule. Acceptable, but worth
flagging — this is the kind of subtlety where "Read the relevant `.sdd`
before editing" (CLAUDE.md:45) is load-bearing.

### 🟢 D6 (low) — Bench-specific rules in `maintain_suite.sdd` not surfaced in `CLAUDE.md`

`vacuous_test` gate scope, `min_added_lines` as a floor, sidecar regrade
via `scripts/regrade_local.py`, the 3× borderline-replication rule —
all only in `maintain_suite.sdd`. `CLAUDE.md` mentions the file as a
pointer (line 43) but doesn't summarize. Same shape as D5: pointer
without restatement, mitigated by the "read the relevant `.sdd`" norm.

### 🟢 D7 (low) — `OMLX_API_KEY` value hardcoded in `CLAUDE.md`

`CLAUDE.md:59` carries the literal API key (`omlx-sdb25582k3mq8pf9`).
This appears intentional for local-dev convenience and the key is for a
localhost service (low-impact if leaked locally), but it does mean:
- The repo carries a credential string;
- Rotation requires editing `CLAUDE.md`;
- No `.sdd` rule prevents accidental external publishing of `CLAUDE.md`
  contents in a future doc-export step.

Probably fine; flagged for awareness, not action.

### 🟢 D8 (low) — `forbids_create` injection mechanism only in `CLAUDE.md`

`CLAUDE.md:70-74` describes the `_inject_forbids_create_sdd` mechanism
(synthetic `<repo>.sdd` written at the cloned-repo root + added to
`.git/info/exclude`). `maintain_suite.sdd` doesn't mention it. The mechanism
is procedural infrastructure rather than an invariant, so single-source is
defensible — but if the injection moves out of `run.py`, the doc would
silently lag.

---

## 1. Topic-coverage matrix

For each topic, where does it appear, and is there agreement?

| Topic | CLAUDE.md | luxe.sdd | tools.sdd | agents.sdd | maintain_suite.sdd | AGENTS.md | Status |
|---|---|---|---|---|---|---|---|
| Mono-only (no swarm/micro/phased) | §49-51 ✓ | Must + Forbids ✓ | — | — | — | **describes retired modes as live** | D1 (contradiction) |
| Single-champion model pin | §6-33 ✓ | only excludes, no positive pin | — | — | — | lists 4 retired models | D2 (sole positive source) |
| Prompts via `prompts.py` | §52-54 ✓ | Must ✓ | — | Must + Must-not ✓ | — | implicit (per-stage prompts) | D4 (triple duplication) |
| `_check_spec_forbids` order | §42 (label only) | — | Must (order specified) ✓ | — | — | — | D5 (order only in one place) |
| `vacuous_test` / `--keep-loaded` / sidecar regrade | §43 (label only) | Must (`--keep-loaded`) ✓ | — | — | Must / Must-not ✓ | — | D6 (summary only in one place) |
| Reflect stage invariants | — | — | — | Must (Track 1) ✓ | — | — | D3 (discoverability) |
| Adaptive policy / score_log ownership | — | — | — | Must (v1.11) ✓ | — | — | D3 (discoverability) |
| `OMLX_API_KEY`, `localhost:8000` | §59 ✓ | — | — | — | — | — | D7 (single source by design) |
| `forbids_create` injection | §70-74 ✓ | — | — | — | — | — | D8 (procedural, single source) |
| `--work-dir` pinned | — | Must ✓ | — | — | Must ✓ | — | OK (two sources agree) |
| `oMLX` `idle_timeout_seconds: 1800` | — | Must ✓ | — | — | — | — | OK (single source) |
| Honesty guards (placeholder / role-path / mass-deletion) | — | — | Must ✓ | — | — | — | OK (single source) |
| `cve_lookup` gated to `task_type == "manage"` | — | — | Must ✓ | — | — | — | OK (single source) |
| Test-coverage rules (new tools / prompts / fixtures) | §66-69 ✓ | — | Must (new honesty guards) ✓ | Must (new prompt variants) ✓ | Must (requirements: block) ✓ | — | OK (split by domain) |

`✓` = present and consistent. Cells reading "implicit" or "describes
retired modes as live" are drift sites.

---

## 2. The conflict bigram in one place

The most concentrated contradiction is between `AGENTS.md` and `luxe.sdd`:

```
AGENTS.md   →  "swarm pipeline's per-stage agents … Architect, Worker (Read/Code/Analyze),
                Validator, Synthesizer …  src/swarm/agents/loop.py:run_agent"
luxe.sdd    →  Forbids: src/swarm/**, src/micro/**, src/phased/**
               Must:    Mono-only execution mode (single capable model, full tool surface)
CLAUDE.md   →  "Mono only. No swarm/micro/phased — they're retired. Don't add
                feature flags to bring them back."
```

The `.sdd` and `CLAUDE.md` are aligned; `AGENTS.md` reads from a different
universe. The "predates v1.0" banner acknowledges this without removing the
hazard.

---

## 3. Recommended consistency-check methodology (for a future lint)

Each item below is **a check, not an implementation**. Listed in
effort-to-value order. Together they cover all 8 findings.

### M1 — Banner-vs-body coherence on instruction docs
- Scan each `*.md` instruction file (`CLAUDE.md`, `AGENTS.md`, future
  `GEMINI.md`, etc.).
- Detect a "stale banner" pattern in the first ~10 lines: keywords like
  `predates`, `deprecated`, `stale`, `do not use`, `historical`.
- If detected, count the lines below that look normative
  (`^- `, `^\d+\.`, sentences with `must`/`do not`/`never`/`use only`).
- **Flag** if the normative body is >X% of the doc (heuristic: X=30).
- Resolution options: (a) move authoritative content into another doc and
  reduce `AGENTS.md` to a 10-line redirect; (b) delete `AGENTS.md`;
  (c) rewrite it for the mono-only architecture so the banner is no longer
  needed. **Catches D1.**

### M2 — Forbids-vs-prose grep
- Parse every `.sdd` for `Forbids:` globs.
- For each forbidden path prefix (e.g., `src/swarm`), grep all `*.md` files
  for that prefix used in a non-negated way (i.e., not in a "do not edit" /
  "retired" / "removed" context — easy heuristic: the prefix appears in a
  bullet that doesn't also contain `retired` / `forbidden` / `removed` /
  `do not`).
- **Flag** matches. **Catches D1** and any future analog.

### M3 — Positive-pin rule
- Parse `CLAUDE.md` (and any other instruction doc) for "pin X" / "use only
  X" / "pinned to X" / "single champion" statements.
- For each, check that **a corresponding Must rule exists in some `.sdd`**.
- **Flag** any pin whose only source is an instruction `.md`. **Catches D2.**

### M4 — Duplicate-statement registry
- Build a normative-sentence list per source by extracting bulleted items
  and `Must:` / `Must not:` items.
- Compute pairwise similarity (token overlap or simple cosine over
  bigrams).
- Cluster near-duplicates across sources.
- For each cluster with members in >1 source, **flag for periodic review**
  ("if you edit one, check the others"). Output: a small drift-bonds list
  consumed by code review. **Catches D4** and any future triplication.

### M5 — Pointer-without-restatement audit
- For each `.sdd` file path mentioned in `CLAUDE.md` (`luxe.sdd`,
  `tools.sdd`, `agents.sdd`, `maintain_suite.sdd`), check whether
  `CLAUDE.md` restates the **headline** rules from each.
- Allow "pointer-only" when the .sdd contains <=Y headline rules
  (Y=3 heuristic); flag when more invariants live behind a single pointer.
- **Catches D5, D6** (and D3, partially — though D3's invariants are
  opt-in mode-specific and may belong in a `MODES.md`).

### M6 — Secret-in-doc scan
- Grep instruction `*.md` for likely secret patterns
  (`[a-z]+-[a-z0-9]{16,}`, `API_KEY=…`, `Bearer …`).
- **Flag** for awareness. Resolution may be "intentional for local dev,
  keep" — but make it explicit. **Catches D7.**

### M7 — Coverage-matrix manifest (optional, longer-term)
- Maintain a small `docs/coverage-matrix.yaml` enumerating canonical
  topics (one row per topic, listing which docs are expected to mention
  it as `primary` or `pointer`).
- A CI check fails if a doc gains a *primary* statement on a topic that
  another doc already owns as primary, without the matrix being updated.
- This is the heaviest mechanism — only worth building once D1/D2 are
  resolved and the doc set is stable.

### Human-cadence backstop
Regardless of automation:
- **Whenever a `.sdd` is edited:** grep instruction `*.md` for any mention
  of the same topic; sync.
- **Whenever an instruction doc is edited:** grep `.sdd` files for the same
  topic; sync.
- **Quarterly:** re-read all instruction docs and `.sdd` files together.
  (This is the cadence that would have caught D1.)

---

## 4. What's *not* in scope here
- We did **not** read every `<dir>/<dir>.sdd` in the tree — only the four
  flagged by `CLAUDE.md` as the chain roots. Other `.sdd` files exist
  deeper in the source (e.g., bench-fixture `.sdd`s injected by `run.py`,
  or per-task `.sdd`s if any) and would need a recursive sweep to be
  thorough. None of them likely contradict the headline rules — but a
  full scan is a one-line `find … -name '*.sdd'` extension to this audit.
- We did not check `RESUME.md` or `lessons.md` against the `.sdd` chain.
  They're historical/operational rather than normative, so contradictions
  there are usually acceptable (they're snapshots of state at a point in
  time). Worth a separate pass if drift becomes an issue.
- We did not propose **wording rewrites**. The point of E5 is to surface
  the drift, not to fix it — fixes are a future implementation session.

---

## 5. Method & ground rules
- Read-only access to `/Users/michaeltimpe/Downloads/luxe/`.
- No model loaded, no benchmarks run, no edits to the luxe tree.
- All outputs written under
  `/Users/michaeltimpe/Downloads/agentic-patterns-luxe-research/`.
- Cross-read all six documents end-to-end, then topic-mapped manually.
  No script needed at this stage; the methodology section (§3) describes
  what a future automated check would do.
