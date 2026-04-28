# Agent Reference

Each pipeline stage uses a specialized agent with a distinct system prompt, tool surface, and model assignment. This document details what each agent does, what it has access to, and how it interacts with the pipeline.

## Agent Loop

All agents share the same execution loop (`src/swarm/agents/loop.py:run_agent`):

```
initialize messages [system_prompt, task_prompt]
for step in range(max_steps):
    check context pressure → elide old tool results if > 0.7
    response = backend.chat(messages, tools)
    if no tool calls in response:
        return response.text as final output
    for each tool call:
        validate arguments against schema
        dispatch tool function (with caching if eligible)
        append result to messages
return final text (or abort reason if max_steps reached)
```

The loop handles two tool-call formats:
- **Structured**: Model returns `tool_calls` in the response (preferred)
- **Text recovery**: Parses `<tool_call>{...}</tool_call>` or bare `{"name": "...", "arguments": {...}}` from response text

Schema validation runs before dispatch — a failed validation returns the error as the tool result, giving the model a chance to self-correct.

---

## Architect

**Purpose**: Decompose a user goal into 4–12 focused micro-objectives, each tagged with a worker role and scope.

| Property | Value |
|----------|-------|
| Model | Qwen2.5-7B-Instruct-4bit |
| Context | 8,192 tokens |
| Max steps | 3 |
| Temperature | 0.3 |
| Tools | None |

**Input**: User goal + task-type-specific decomposition prompt + repo survey (languages, LOC, file counts).

**Output**: JSON array of micro-objectives:
```json
[
  {"title": "Survey repo structure", "role": "worker_read", "expected_tools": 3, "scope": "."},
  {"title": "Run security scanner on auth module", "role": "worker_analyze", "expected_tools": 2, "scope": "src/auth/"},
  {"title": "Fix SQL injection in query builder", "role": "worker_code", "expected_tools": 4, "scope": "src/db/query.py"}
]
```

**Role assignment rules**:
- `worker_read` — file reading, grepping, structure exploration
- `worker_analyze` — linting, type checking, security scanning
- `worker_code` — writing, editing, creating files

The architect does NOT produce validator or synthesizer entries — those are appended automatically by the orchestrator.

**Fallback**: If the model returns invalid JSON or an empty array, the system falls back to a single `worker_read` task covering the entire goal.

---

## Worker (Read)

**Purpose**: Gather information from the codebase — read files, search patterns, inspect git history.

| Property | Value |
|----------|-------|
| Model | DeepSeek-Coder-V2-Lite-Instruct-4bit |
| Context | 16,384 tokens |
| Max steps | 6 |
| Temperature | 0.2 |
| Tools | read_file, list_dir, glob, grep, git_diff, git_log, git_show |

**System prompt directives**:
- Be focused and efficient — each tool call should serve a clear purpose
- Report findings as structured observations with `file:line` citations
- Do not make changes to any files
- If you cannot find what you're looking for, say so clearly

**Context augmentation**: Receives recency-weighted prior findings from earlier subtasks:
- Immediately preceding subtask: full result (800 chars)
- 2–3 subtasks back: 400 chars
- 4+ subtasks back: one-line summary (title + done/blocked + finding count)

**Tool details**:

| Tool | Description | Cacheable |
|------|-------------|-----------|
| `read_file` | Read file contents with line numbers, supports offset/limit | Yes |
| `list_dir` | List directory contents (max 150 entries) | Yes |
| `glob` | Find files matching a glob pattern | Yes |
| `grep` | Regex search via ripgrep (or fallback), max 150 results | Yes |
| `git_diff` | Show git diff (optional: staged, ref, path filter) | No |
| `git_log` | Recent commits in oneline format (default 20) | Yes |
| `git_show` | Commit details with stat and metadata | Yes |

---

## Worker (Code)

**Purpose**: Write, edit, or create code files to fulfill a specific objective.

| Property | Value |
|----------|-------|
| Model | Qwen2.5-Coder-14B-Instruct-MLX-4bit |
| Context | 32,768 tokens |
| Max steps | 10 |
| Temperature | 0.2 |
| Tools | read_file, write_file, edit_file, list_dir, glob, grep, git_diff, git_log, bash |

**System prompt directives**:
- Read relevant code first to understand context before making changes
- Make minimal, focused changes — only what the objective requires
- Cite every file modified with its path
- Test changes if possible (run linters, type checkers)
- Do not refactor beyond what the objective asks for

**Additional tools beyond Worker (Read)**:

| Tool | Description | Cacheable |
|------|-------------|-----------|
| `write_file` | Write content to a file (creates parent directories) | No |
| `edit_file` | String replacement (unique match or replace_all) | No |
| `bash` | Allowlisted shell execution (cargo, pytest, ruff, npm, etc.) | No |

**Shell allowlist**: cargo, cat, echo, find, git, go, grep, head, ls, make, npm, npx, pip, pytest, python, ruff, sed, sort, tail, tree, wc.

All filesystem operations are scoped to the repo root. Path traversal attempts raise `PermissionError`.

---

## Worker (Analyze)

**Purpose**: Run static analysis tools and inspect code for bugs, security issues, and quality problems.

| Property | Value |
|----------|-------|
| Model | Qwen2.5-Coder-14B-Instruct-MLX-4bit |
| Context | 32,768 tokens |
| Max steps | 8 |
| Temperature | 0.2 |
| Tools | read_file, list_dir, glob, grep, git_diff, git_log, lint, typecheck, security_scan, deps_audit |

**System prompt directives**:
- Use the appropriate analysis tools for the language
- For each finding, verify it by reading the relevant code
- Report findings with exact `file:line` citations
- Classify severity: critical, high, medium, low, info
- Do not make changes to any files

**Analysis tools** (language-gated — only tools matching detected languages are loaded):

| Tool | Languages | Backend | Cacheable |
|------|-----------|---------|-----------|
| `lint` | Python | ruff | Yes |
| `typecheck` | Python | mypy | Yes |
| `security_scan` | Python | bandit | Yes |
| `deps_audit` | Python | pip-audit | Yes |
| `lint_js` | JavaScript, TypeScript | eslint | Yes |
| `typecheck_ts` | TypeScript | tsc | Yes |
| `lint_rust` | Rust | clippy | Yes |
| `vet_go` | Go | go vet | Yes |

All analysis tools return a uniform `{findings: [...], count: N}` JSON payload, capped at 150 findings.

---

## Validator

**Purpose**: Verify that citations and findings from worker agents are accurate.

| Property | Value |
|----------|-------|
| Model | Qwen2.5-7B-Instruct-4bit |
| Context | 8,192 tokens |
| Max steps | 6 |
| Temperature | 0.1 |
| Tools | read_file, grep |

**System prompt directives**:
- For each finding with a `file:line` citation, confirm the file exists and the cited code matches
- Keep verified findings with original text
- Remove findings where the file doesn't exist or the cited code doesn't match
- Flag findings that cannot be fully verified with `[UNVERIFIED]`
- Do NOT add new findings — only verify or remove
- Preserve original severity classifications

**Output format**:
```
## Verified Findings
(findings that passed citation checks)

## Removed Findings
(findings that failed verification, with reason)

## Verification Summary
- Total findings checked: N
- Verified: N
- Removed: N
- Unverified (kept with flag): N
```

The validator uses the same `ToolCache` as the workers, so file reads already performed by workers are served from cache.

---

## Synthesizer

**Purpose**: Assemble validated findings into a final, severity-grouped report.

| Property | Value |
|----------|-------|
| Model | Qwen2.5-32B-Instruct-4bit |
| Context | 32,768 tokens |
| Max steps | 3 |
| Temperature | 0.3 |
| Tools | None |

The synthesizer is the only stage that uses the 32B model, and it runs exactly once. It has no tools — it works entirely from the validated findings passed to it.

**Task-type-specific output formats**:

### review / bugfix
```
# Code Review Report
## Critical Issues
## High Priority
## Medium Priority
## Low Priority / Suggestions
## Summary
- Total findings: N
- Critical: N | High: N | Medium: N | Low: N
- Files analyzed: (list)
- Key recommendations: (2-3 bullet points)
```

### implement
```
# Implementation Summary
## Changes Made
## Testing
## Remaining Work
## Notes
```

### default (document, summarize, manage)
Clear, well-organized report preserving all `file:line` citations with deduplication.

---

## Escalation

When a worker subtask fails (aborted or schema_rejects > 3), the orchestrator attempts escalation:

```
worker_read (2.4B) → worker_analyze (14B) → worker_code (14B)
```

Escalation:
- Retries the same objective with a more capable role
- Uses a larger model with a broader tool surface
- Logs the escalation as a structured event
- Limited to 1 retry per subtask (configurable via `escalation.max_retries`)

If escalation also fails, the subtask is marked as `blocked` and the pipeline continues with remaining subtasks.

---

## Tool Caching

A single `ToolCache` instance is shared across all worker and validator subtasks within a pipeline run. Read-only tools are cached by `(tool_name, arguments_hash)`:

**Cacheable tools**: read_file, list_dir, glob, grep, git_log, git_show, and all analysis tools.

**Never cached**: write_file, edit_file, bash, git_diff (may change between calls).

Cache metrics (hits/misses) are tracked per-run and reported in the metrics summary.
