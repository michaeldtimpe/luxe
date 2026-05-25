# Forge ↔ Luxe ↔ Micro-mind: Overlap & Cross-pollination Analysis

## Context

You maintain two agentic-LLM projects — **luxe** (Python, MLX-only repo maintainer on Qwen3.6-35B-A3B) and **micro-mind** (Rust REPL on qwen25-1.5b via llama.cpp). The public project **forge** (Antoine Zambelli, `github.com/antoinezambelli/forge`, v0.6.0 April 2026) targets the same problem space from a different angle: a general-purpose Python *library* that makes small (~8B) local models reliable for agentic workflows via guardrails, context compaction, and backend adapters.

This document maps the conceptual overlap, calls out ideas worth porting in either direction, and ends with concrete candidate work items you could pick up if any of them interest you.

---

## What forge is, in one paragraph

A Python 3.12+ library (Pydantic + httpx, minimal deps) for orchestrating tool-using LLM workflows. Three integration modes: standalone `WorkflowRunner`, OpenAI-compatible proxy server, or à-la-carte middleware. Core abstractions: `WorkflowRunner` (loop), `ContextManager` with three compaction strategies (`NoCompact` / `SlidingWindowCompact` / `TieredCompact`), `StepTracker` (control flow outside message history), guardrails (`ResponseValidator`, `StepEnforcer`, `ErrorTracker`), client adapters (`OllamaClient`, `LlamafileClient`, `AnthropicClient`), and `SlotWorker` (priority-preemptive queueing onto a single inference slot). Backed by 865 unit tests, 26-scenario tiered eval suite, IEEE preprint, 86.5% accuracy reported on Ministral-3 8B.

Source modules confirmed at `src/forge/{core,guardrails,clients,context,prompts,tools,proxy}/`.

---

## High-level overlap map

| Concern | Forge | Luxe | Micro-mind |
|---|---|---|---|
| Agent loop | `core/runner.py::WorkflowRunner` | `src/luxe/agents/loop.py` (~1900 lines) | `src/agent/mod.rs::run_turn` |
| Tool dispatch | `core/workflow.py` + `tools/` | `src/luxe/tools/` (`base.py` dispatch) | `src/tools/` dispatch |
| Context compaction | 3-phase `TieredCompact` at 75% | elide tool results at 70% pressure | elide at 70% pressure (`src/agent/context.rs`) |
| Malformed tool-call recovery | `ResponseValidator` (rescue parsing) | parser + retries in loop | parser + coach (`src/agent/coach.rs`) |
| Premature-finish prevention | `StepEnforcer` 3-tier nudges | `spec_validator.py` requirements | read-before-write + auto-read recovery |
| Retry budgeting | `ErrorTracker` (resets on progress) | body-aware retry in `backend.py` | turn cap + write-pressure exit |
| Backend abstraction | client adapter protocol (4 backends) | oMLX only (intentional) | llama-server only (intentional) |
| Eval suite | 26 scenarios, tiered difficulty | maintain_suite + SWE-bench n=75 + BFCL n=1240 | 13 fixtures + 4-axis predicate framework |
| Single-slot GPU sharing | `SlotWorker` priority queue | flock-based per-repo serialization | n/a (single REPL) |

All three converge on the same insight — *small models need a smarter harness, not more parameters* — but partition the problem differently: forge is a horizontal library, luxe is a vertical PR-producing product, micro-mind is a hardened harness for one model.

---

## Ideas in forge worth porting into luxe

### 1. Three-phase `TieredCompact` (forge `context/strategies.py`)
Luxe currently elides old tool results when context pressure crosses ~70%. Forge's tiered approach is more principled: phase 1 drops nudges, phase 2 truncates tool results, phase 3 drops reasoning — with `MessageMeta` priorities ensuring tool *calls* are never cut. This maps cleanly onto luxe's loop because luxe already has rich message types (read results vs. write results vs. spec-validation messages). Worth borrowing the *priority taxonomy* even if luxe keeps its single-phase elision.

Candidate target: `src/luxe/agents/loop.py` elision block; introduce a `compact_priority` field on the message dataclass.

### 2. `StepEnforcer` with escalating nudge tiers (forge `guardrails/step_enforcer.py`)
Luxe's `outcomes.py` taxonomy classifies failures but the intervention is implicit in the loop. Forge formalizes a three-tier nudge cascade (gentle → firmer → forceful) that coerces a small model to take the next required step without resampling. Luxe could replace ad-hoc "policy-scored interventions" with this pattern — particularly the "expects_zero_calls" requirement enforcement.

Candidate target: `src/luxe/spec_validator.py` + new module `src/luxe/agents/nudge.py`.

### 3. `SlotWorker` priority queue (forge `core/slot_worker.py`)
Luxe uses `flock` per-repo to serialize runs. That prevents two runs on the same repo, but not contention across repos for the single loaded MLX model. `SlotWorker` would let you queue work onto the MLX slot with priority preemption — relevant if you ever run `pr watch` + a new `maintain` concurrently.

Candidate target: new module wrapping `src/luxe/backend.py`.

### 4. OpenAI-compatible proxy server mode (forge `proxy/`)
Luxe is single-entry-point (CLI). Forge ships a `forge-proxy` that intercepts OpenAI calls and applies guardrails transparently. If you ever want luxe's guardrails to wrap a non-luxe consumer (say, Continue/Aider talking to oMLX), the proxy pattern would unlock that without re-architecting.

### 5. Per-model sampling defaults (forge v0.6.0)
Luxe pins one model and so doesn't need this, but if/when you test on a second model in `benchmarks/`, forge's `core/inference.py` per-family defaults are a clean template.

### 6. Anthropic client adapter (forge `clients/anthropic.py`)
Could let you route a hard subtask in luxe to Claude for ground-truth checks during evaluation — without touching the core loop. Pattern is portable even if you don't import the file.

---

## Ideas in forge worth porting into micro-mind

### 1. Client adapter protocol (forge `clients/base.py::LLMClient`)
Micro-mind talks only to llama-server today via `ureq`. Adopting forge's adapter protocol shape (in Rust: a trait `LlmClient` with `chat_stream`) would let you swap to Ollama or MLX without changing `src/agent/mod.rs`. Low effort, high portability payoff.

Candidate target: refactor `src/llm/` to expose a trait, push `ureq`-based llama-server into one impl.

### 2. `ErrorTracker` with progress-reset semantics (forge `guardrails/error_tracker.py`)
Micro-mind has write-pressure exit (3 zero-byte non-writes → break) and turn cap. Forge's `ErrorTracker` tracks consecutive retries *and resets on progress*, which is a strictly more nuanced signal than monotonic counters. Useful if you ever loosen the 8-turn cap.

### 3. Three-tier nudge escalation (forge `prompts/nudges.py` + `StepEnforcer`)
Micro-mind has pattern-matched coaching (`src/agent/coach.rs`) but no *escalation*. Adopting a tiered nudge pattern for the read-before-write violation (current single-shot auto-read at 87%) could push the remaining 13% closer to floor.

### 4. Hardware-aware budgeting (forge `context/hardware.py::detect_hardware`)
Trivial for the 1.5B case, but interesting if you ever ship a Linux/CUDA variant of micro-mind. Pattern: budget = f(VRAM tier) rather than a fixed constant.

### 5. Tiered compaction priorities for elision
Same idea as for luxe: micro-mind elides on age, but message priority (reasoning > result, write-result > read-result) is a cleaner basis.

---

## Ideas from luxe / micro-mind that could feed back into forge

Forge is library-shaped and intentionally minimal. These are patterns from your work that forge does *not* have and which fit its scope:

1. **Outcome taxonomy** (luxe `agents/outcomes.py`) — forge's `ErrorTracker` counts failures but doesn't classify them. A first-class enum of outcomes (`STRONG_GOLD_MATCH`, `WRONG_TARGET`, `EMPTY_PATCH_TIMEOUT`, etc.) makes for much better telemetry. Likely accepted upstream.
2. **Auto-read on read-before-write refusal** (micro-mind `src/agent/mod.rs`, b-toolresult shape) — sophisticated synthetic-recovery pattern. Forge has nudges but not synthetic tool-result injection.
3. **JSONL provenance schema with `ToolOrigin`** (micro-mind `src/obs/`) — distinguish model output from synthetic guard recoveries in traces. Forge's tracing story is thinner.
4. **Body-aware backend retry** (luxe `backend.py`) — distinguishes `loading`/`swapping` (retry) from `unavailable`/`crashed`/`oom` (fail fast). Forge has retry logic but not error-class differentiation.
5. **Four-axis predicate framework** (micro-mind `bench/`) — kind × count × provenance × compositionality. Generalizes forge's 26-scenario suite.
6. **Convergence scoring** (luxe `agents/convergence.py`) — path-diversity-based stuck-loop detection. Complements forge's `ErrorTracker`.

If you ever decide to upstream contributions, items 1, 2, 3 are the cleanest fits for forge's existing architecture.

---

## Where the projects are *not* overlapping (and shouldn't try to)

- Forge has no code-aware retrieval. Luxe's `search.py` (BM25) and `symbols.py` (tree-sitter for Py/JS/TS/Rust/Go) are vertical features tied to "edit a repo" — out of scope for a general agent library.
- Forge has no PR cycle, no SpecDD contracts, no CVE lookup. Luxe-specific.
- Micro-mind deliberately omits multi-turn chain recovery, MCP, tree-sitter, BM25, glob — because routing entropy kills the 1.5B model. Forge's `StepEnforcer` is closer to micro-mind's territory but designed for 8B-class behavior; expect adaptation, not a drop-in.

---

## Suggested next actions (pick zero or more)

Ordered by leverage-per-effort:

1. **Lift `TieredCompact` priorities into luxe's elision.** Smallest change, highest immediate value. Touch points: `src/luxe/agents/loop.py` (elision block) + add a `compact_priority` field to the message dataclass. ~half-day.
2. **Refactor micro-mind to a Rust `LlmClient` trait.** Unlocks future backend variety, mirrors a forge pattern in spirit not code. Touch points: `src/llm/`. ~half-day.
3. **Adopt three-tier nudge cascade in luxe `spec_validator.py`.** Replaces ad-hoc policy scoring with a forge-style escalation. ~1 day.
4. **Add micro-mind's auto-read recovery shape to luxe's loop.** Same idea solves "I tried to edit without reading" in luxe too. ~1 day.
5. **Write a `forge`-compatible adapter for luxe's oMLX backend** (a 100-line `LLMClient` subclass that calls `src/luxe/backend.py`). Lets you A/B-test luxe's loop against forge's loop on the same model + tasks — *the cleanest experiment to figure out which architecture wins for your specific stack.* ~1–2 days.
6. **Upstream luxe's outcome taxonomy as a forge PR.** Community contribution; clarifies their tracing. ~1 day.

---

## Verification (if you act on any of the above)

- Luxe changes: run `benchmarks/maintain_suite/run.py` and the BFCL/SWE-bench harnesses; ensure no regression on v1.6.1 numbers (BFCL n=1240 agent mode 90.24%).
- Micro-mind changes: micro-mind is paused. Reopen condition per its CLAUDE.md is "luxe shipped X, can we port it?" — so any micro-mind work should be triggered by a luxe-side win first.
- Forge experiments (action 5): build a small harness in a scratch dir under `~/Downloads/forge-luxe-research/`; do not pollute luxe or micro-mind trees until the experiment shows signal.

---

## Critical files referenced

**Forge** (`src/forge/`):
- `core/runner.py::WorkflowRunner`, `core/slot_worker.py::SlotWorker`, `core/steps.py::StepTracker`, `core/inference.py::run_inference`
- `guardrails/{response_validator,step_enforcer,error_tracker}.py`
- `clients/{base,ollama,llamafile,anthropic}.py`
- `context/{manager,strategies,hardware}.py`
- `proxy/proxy.py::ProxyServer`

**Luxe** (`/Users/michaeltimpe/Downloads/luxe/`):
- `src/luxe/agents/{loop.py,outcomes.py,convergence.py,prompts.py}`
- `src/luxe/{backend.py,spec_validator.py,citations.py,pr.py,locks.py}`
- `src/luxe/tools/{base.py,fs.py,git.py}`
- `src/luxe/{search.py,symbols.py}` (out of scope for forge)

**Micro-mind** (`/Users/michaeltimpe/Downloads/micro-mind/`):
- `src/agent/{mod.rs,guards.rs,coach.rs,compress.rs,context.rs}`
- `src/tools/{mod.rs,fs_write.rs,fs_utils.rs}`
- `src/llm/{prompt.rs,chat.rs}` (candidate trait refactor target)
- `src/obs/`, `bench/` (audit + 4-axis predicate framework)
