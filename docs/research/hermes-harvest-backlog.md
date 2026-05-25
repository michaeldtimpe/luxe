# Hermes Agent → Feature Backlog

## Executive Summary

Hermes Agent (Nous Research) differentiates from predecessor frameworks by closing the agent loop on itself: a reflective phase emits portable, agentskills.io-compatible procedural memory that compounds across sessions, while a pluggable three-layer memory keeps episodic, semantic, and procedural state independently swappable. Around that core sits a 7+ backend execution abstraction (local, Docker, SSH, Singularity, Modal, Daytona, Vercel Sandbox), a first-class MCP integration, a multi-channel gateway that fans one agent process across CLI, Telegram, Discord, Slack, WhatsApp, Signal, and Email, and a TUI with streaming output. The harvest below mines that surface area for a target-project feature backlog along four pillars — Architecture & Agent Loop, Skill & Memory Systems, Tooling & Execution Backends, Platform/Gateway & DX — plus an explicit anti-patterns list capturing failure modes the Medium article calls out.

**Sources:** Medium article (sathishkraju, *"I switched from OpenClaw to Hermes Agent: here's what nobody told me"*) and `NousResearch/hermes-agent` GitHub repository (file structure verified via `gh api` against live `main`).

---

## Pillar 1 — Architecture & Agent Loop

[HERMES-001] Reflective Phase as first-class loop stage
Type: feature
Source: article § core differentiator; `run_agent.py`, `agent/`
Why: Adds a post-execute analysis step that turns one-shot runs into self-evaluating runs — the central Hermes vs OpenClaw differentiator.
Acceptance hints: Loop is `plan → execute → reflect → emit`; reflect stage has access to the trace and tool outputs and produces a structured evaluation payload.
Risk/Caveat: Reflection burns tokens on every run; gate behind a budget cap and a complexity threshold.

[HERMES-002] Closed learning-loop wiring (execute → reflect → skill emit → reuse)
Type: feature
Source: article; `agent/`, `skills/`
Why: Connects reflection to a SkillEmitter so successful runs deposit reusable procedural memory the agent can pattern-match against later.
Acceptance hints: Demonstrate one task whose skill is created on first run and *used* on a second, semantically similar run; verify via `result.skills_used`.
Risk/Caveat: Without dedup, near-duplicate skills will accumulate and degrade retrieval precision.

[HERMES-003] Skill-promotion self-evaluation gate
Type: policy
Source: article (skills should generalize, not encode one-offs)
Why: Stops fragile, environment-specific traces from polluting the persistent skill library.
Acceptance hints: A generalization rubric (mock-context replay or LLM-judge) must pass before a draft skill is written to persistent storage.
Risk/Caveat: Overly strict gating suppresses useful narrow automations; tune false-negative rate.

[HERMES-004] Default-off self-learning safety policy
Type: policy
Source: article ("#1 nobody told me" gotcha)
Why: Silent persistence and autonomous skill generation must be opt-in to prevent drift, surprise spend, and audit gaps.
Acceptance hints: `persistent_memory=false` and `skill_generation=false` ship as defaults; an obvious startup banner notes self-learning is disabled until enabled.
Risk/Caveat: First-run users may believe the agent is broken — pair with onboarding copy and a `hermes setup` prompt.

[HERMES-005] Run-result transparency primitives
Type: feature
Source: article ("Skills used: [...] Task completed 40% faster")
Why: Operators need to see exactly which skills the agent synthesized vs reused on every run to trust the closed loop.
Acceptance hints: Every run returns structured `result.skills_created` and `result.skills_used` arrays alongside the normal output payload.
Risk/Caveat: Verbose traces overwhelm casual CLI users — hide by default, expose via `--verbose` or `/run-meta`.

[HERMES-006] Namespace-isolated subagent delegation
Type: feature
Source: `agent/`, `hermes_state.py`
Why: Enables parallel workstreams without parent/child memory or tool-state collisions.
Acceptance hints: Subagent spawner produces a scoped state context with a unique namespace prefix; parent receives only declared return values.
Risk/Caveat: Nested delegation makes cost attribution and failure diagnosis significantly harder — emit per-subagent telemetry.

---

## Pillar 2 — Skill & Memory Systems

[HERMES-007] Adopt agentskills.io markdown skill format
Type: feature
Source: article; `skills/`
Why: A portable, file-based skill format avoids vendor lock-in and lets skills move between agent runtimes.
Acceptance hints: Skills serialize to markdown with explicit frontmatter (`name`, `description`, `requirements`, `provenance`); a schema validator gates import/export.
Risk/Caveat: External standard evolution can force migrations — keep an internal `format_version` field.

[HERMES-008] Three-layer memory architecture (episodic / semantic / procedural)
Type: feature
Source: article
Why: Separating recent context, distilled concepts, and skill library improves retrieval quality vs a monolithic store.
Acceptance hints: A coordinator exposes independent `read/write/search` APIs per layer with documented retrieval weighting.
Risk/Caveat: Three layers competing for the same prompt budget — define an explicit allocation policy.

[HERMES-009] Pluggable memory backend interface
Type: feature
Source: article (v0.10.0); `RELEASE_v0.10.0.md`, `providers/`
Why: Avoid hard dependency on a single persistence stack — support file, vector, and external services (Honcho).
Acceptance hints: Define `BaseMemoryProvider` with `read`, `write`, `search`; ship a local-file default and at least one external adapter.
Risk/Caveat: Lowest-common-denominator API can hide advanced backend features — allow capability flags.

[HERMES-010] Honcho dialectic user modeling
Type: spike
Source: article
Why: Persistent profiles of formatting preferences, detail levels, and past decisions raise long-term interaction quality.
Acceptance hints: Capture explicit feedback + observed contradictions into a profile schema reused across sessions.
Risk/Caveat: User modeling raises privacy and retention concerns — keep profiles local-first and user-deletable.

[HERMES-011] FTS5 session search + LLM summarization
Type: feature
Source: article; `hermes_state.py`
Why: Cross-session recall needs both fast keyword lookup and LLM-generated summaries of matching threads.
Acceptance hints: SQLite FTS5 indexes session text; a summarization pass renders matches into context-ready snippets.
Risk/Caveat: Concurrent writes hit FTS5 lock contention under multi-agent load — batch writes.

[HERMES-012] Human-readable persona/context files
Type: policy + feature
Source: article ("opaque SQLite is a regression")
Why: Operators need to inspect, edit, and `git diff` persistent state — markdown wins over a binary blob.
Acceptance hints: `USER.md`, `MEMORY.md`, and `SOUL.md` (persona) live at the project root and auto-load at startup; SQLite is restricted to FTS5 indexing only.
Risk/Caveat: External hand-edits can break parsers — keep schema permissive and validate non-destructively.

[HERMES-013] First-party optional-skills distribution channel
Type: feature
Source: live-repo finding — `optional-skills/` exists as a sibling of `skills/`
Why: Separating curated, opt-in skill bundles from core skills lets the project ship optional capabilities without inflating the default surface area.
Acceptance hints: Two skill directories with distinct loaders; user-facing CLI to enable individual optional skills.
Risk/Caveat: Discoverability gap — optional skills users don't know about deliver no value.

---

## Pillar 3 — Tooling & Execution Backends

[HERMES-014] Modular toolset registry with namespacing
Type: feature
Source: `toolsets.py`, `toolset_distributions.py`
Why: Grouping tools into named bundles beats a flat enable/disable list — scales as the tool surface grows and protects prompt budget.
Acceptance hints: Tools register into named bundles (e.g., `fs`, `git`, `net`); CLI loads/unloads bundles; each bundle declares its own permission scope.
Risk/Caveat: Cross-bundle tool dependencies become brittle — keep dependencies explicit and shallow.

[HERMES-015] Curated starter toolset presets
Type: feature
Source: `tools/`, `toolset_distributions.py`
Why: A new install needs a sensible default — define `core`, `developer`, and `research` presets so users aren't drowning in 40+ tools.
Acceptance hints: Each preset documents which bundles ship enabled; switching presets is a single CLI command.
Risk/Caveat: Presets drift from real usage — instrument tool-call frequency to revise presets quarterly.

[HERMES-016] Multi-backend execution abstraction
Type: feature
Source: `providers/`, `docker/`, `docker-compose.yml`
Why: A uniform execution API across local, Docker, SSH, Singularity, Modal, Daytona, and Vercel Sandbox prevents infrastructure lock-in.
Acceptance hints: One execution interface; functional adapters for Local + Docker + Modal as the first wave.
Risk/Caveat: Filesystem and networking semantics diverge across backends — write a parity test suite.

[HERMES-017] Serverless hibernation for idle agents
Type: spike
Source: article (Modal/Daytona hibernation); `providers/`
Why: Persistent agents racking up idle cost is an anti-pattern — hibernation enables near-zero cost between interactions.
Acceptance hints: Snapshot agent state on idle, hydrate on incoming event; measure cold-start latency and target < 2s.
Risk/Caveat: Wake latency hurts interactive UX — keep a warm pool for active sessions.

[HERMES-018] First-class MCP integration (adoption, not greenfield)
Type: feature
Source: live-repo finding — `mcp_serve.py` (~31k LoC) already at repo root
Why: MCP is already a first-class concern in Hermes — treat this as adopting their adapter shape, not building one.
Acceptance hints: Register external MCP servers via config; expose discovered tools through the existing toolset registry.
Risk/Caveat: Third-party MCP servers expand the attack surface — gate by allowlist and capability scopes.

[HERMES-019] RPC-based Python script tool wrapping
Type: feature
Source: article (RPC interface for Python integration)
Why: Lets users promote ad-hoc Python scripts into agent tools without writing a full tool implementation.
Acceptance hints: A wrapper reads type hints + docstrings, generates a JSON schema, registers an RPC-callable tool.
Risk/Caveat: Arbitrary script execution is RCE-by-design — require an isolated execution backend (HERMES-016).

[HERMES-020] Trajectory compression + datagen pipeline (integration, not build)
Type: feature
Source: live-repo finding — `trajectory_compressor.py` (~65k LoC), `batch_runner.py` (~57k LoC), `datagen-config-examples/`
Why: These are production-grade files — wire them in rather than reinventing.
Acceptance hints: Pipeline ingests run histories, scrubs secrets, emits Alpaca/ShareGPT-format datasets.
Risk/Caveat: Trajectories leak credentials and PII by default — multi-layer scrubbing is mandatory.

[HERMES-021] ACP (Agent Communication Protocol) adapter layer
Type: spike
Source: live-repo finding — `acp_adapter/`, `acp_registry/`
Why: An emerging agent-to-agent protocol layer worth evaluating before committing to a proprietary cross-agent shape.
Acceptance hints: Prototype calling an external ACP-speaking agent and registering one of our own.
Risk/Caveat: Protocol is young — implementation may churn; keep adapter behind a feature flag.

---

## Pillar 4 — Platform / Gateway & DX

[HERMES-022] Unified multi-channel gateway runtime
Type: feature
Source: article; `gateway/`
Why: One agent process fronts CLI + Telegram + Discord + Slack + WhatsApp + Signal + Email — operationally simpler than per-channel daemons.
Acceptance hints: A gateway daemon maintains independent routing instances per channel sharing one agent core.
Risk/Caveat: Per-platform rate limits and formatting quirks leak into the core — define a capability descriptor per adapter.

[HERMES-023] Advanced TUI workspace
Type: feature
Source: `tui_gateway/`, `ui-tui/`
Why: Power users want a terminal-native UX with multiline editing, slash-command completion, and streaming tool output.
Acceptance hints: TUI streams tool stdout/stderr live; multi-line input supported; slash-autocomplete from the toolset registry.
Risk/Caveat: Terminal rendering quirks across Windows + WSL + macOS — pin a known-good rendering stack.

[HERMES-024] Guided setup wizard
Type: feature
Source: article; `setup-hermes.sh`, `cli-config.yaml.example`
Why: First-run friction kills adoption — a guided wizard validates API keys, picks a backend, writes config.
Acceptance hints: `hermes setup` (or equivalent) probes provider creds, confirms at least one execution backend works, writes a validated config file.
Risk/Caveat: Wizard logic rots as config grows — keep wizard schema-driven so it stays current.

[HERMES-025] Granular `config set` mutation CLI
Type: feature
Source: article (`hermes config set`)
Why: Editing a sprawling YAML by hand is error-prone; per-key CLI mutation is auditable and scriptable.
Acceptance hints: `config set <key> <value>` with schema validation; `config get` and `config diff` companions.
Risk/Caveat: Drift between hand-edited file and CLI writes — pick one source of truth.

[HERMES-026] Persona switching via slash command
Type: feature
Source: article (`/personality [name]`)
Why: Mid-conversation persona changes (architect → writer → reviewer) without restarting the session.
Acceptance hints: `/personality <name>` hot-reloads from `SOUL.md`-style files; current persona is visible in the prompt.
Risk/Caveat: Persona changes mid-task confuse multi-step loops — refuse persona switches during active tool sequences.

[HERMES-027] Natural-language cron scheduler
Type: feature
Source: article; `cron/`
Why: Schedule recurring tasks ("every Friday at 4pm summarize the repo") without learning cron syntax.
Acceptance hints: Parser translates natural-language schedules into cron; `hermes schedule list/add/remove` CLI.
Risk/Caveat: Ambiguous phrases ("end of month") produce unsafe automations — require a confirmation dry-run.

[HERMES-028] Migration toolkit with `--dry-run`
Type: feature
Source: article (`hermes claw migrate --dry-run`)
Why: Migration friction is the single biggest blocker for switching from a competing/legacy tool.
Acceptance hints: Migration CLI imports personas, memories, skills, API keys, allowlists; `--dry-run` prints a diff without writing.
Risk/Caveat: Partial migrations leave inconsistent state — wrap in a transaction-style commit.

[HERMES-029] Secure-installer alternatives
Type: policy
Source: `setup-hermes.sh`, `SECURITY.md`, article
Why: Curl-pipe-bash alone undermines operator trust — ship signed binaries and a package-manager path.
Acceptance hints: Release pipeline produces signed binaries plus at least one package-manager target (Homebrew or apt) alongside the bootstrap script.
Risk/Caveat: Multi-target release engineering is heavy — start with one signed channel and expand.

[HERMES-030] Cross-platform runtime matrix CI
Type: feature
Source: article; `constraints-termux.txt`, `flake.nix`, `nix/`
Why: Hermes already supports Linux/macOS/WSL2/Windows/Termux — match that breadth with CI proof.
Acceptance hints: CI runs core smoke tests on Linux, macOS, WSL2, and at least one of {native Windows, Termux} per release.
Risk/Caveat: Native Windows path semantics and terminal codes diverge — keep an explicit override layer.

---

## Skip / Anti-Patterns

[SKIP-001] Opaque SQLite-only memory
Source: article ("the SQLite mistake")
Why to skip: Binary memory blocks audit, `git diff`, manual edits, and operator trust.
Preferred: File-based memory (markdown/JSONL) as the canonical store; SQLite restricted to FTS5 search indexing only.

[SKIP-002] Unsandboxed marketplace skill ingestion
Source: article (ClawHub ~12% malware rate); `SECURITY.md`
Why to skip: Community skill ecosystems become malware delivery channels at scale.
Preferred: Static analysis + signed-provenance + sandboxed execution (HERMES-016 backends) before any third-party skill runs.

[SKIP-003] Curl-pipe-bash as the only install path
Source: `setup-hermes.sh`; article
Why to skip: Unsigned bootstrap erodes operator trust and fails security review at most enterprises.
Preferred: Ship signed binaries and a package-manager channel alongside the bootstrap script (see HERMES-029).

[SKIP-004] Treating "zero CVEs" as security proof
Source: article (explicit caveat: "less exposure ≠ inherently more secure"); `SECURITY.md`
Why to skip: Low exposure time produces low CVE counts independent of real posture; it's a vanity metric.
Preferred: Threat model the agent loop, sandbox tool execution, scope tool permissions, and rehearse incident response — bake all of this in early.

[SKIP-005] Unbounded autonomous retry loops
Source: `agent/`, general agent-framework lesson
Why to skip: Recursive retries amplify cost, latency, and failure blast radius.
Preferred: Per-tool retry budgets, circuit breakers on repeated identical failures, and explicit escalation paths.

---

## Coverage Metrics

| Pillar | Target | Delivered | Status |
|---|---|---|---|
| P1 · Architecture & Agent Loop | ≥ 5 | 6 | ✅ |
| P2 · Skill & Memory Systems | ≥ 5 | 7 | ✅ |
| P3 · Tooling & Execution Backends | ≥ 5 | 8 | ✅ |
| P4 · Platform / Gateway & DX | ≥ 5 | 9 | ✅ |
| Skip / Anti-Patterns | ≥ 3 | 5 | ✅ |
| **Total backlog inventory** | 25–35 | **35** | ✅ |

**Live-repo paths verified against `NousResearch/hermes-agent@main` via `gh api` (May 2026):**
`agent/`, `skills/`, `optional-skills/`, `tools/`, `providers/`, `gateway/`, `tui_gateway/`, `ui-tui/`, `cron/`, `acp_adapter/`, `acp_registry/`, `mcp_serve.py`, `trajectory_compressor.py`, `batch_runner.py`, `datagen-config-examples/`, `run_agent.py`, `hermes_state.py`, `toolsets.py`, `toolset_distributions.py`, `setup-hermes.sh`, `cli-config.yaml.example`, `flake.nix`, `nix/`, `constraints-termux.txt`, `RELEASE_v0.10.0.md`, `SECURITY.md`.
