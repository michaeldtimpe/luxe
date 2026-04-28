# Luxe: Paged Prefix Sharing + MCP + MLX-JANG Implementation Plan

## Context

Luxe's current architecture uses uniform 4-bit MLX quantization, a custom tool system, and basic prompt-cache config flags without telemetry. Three upgrades unlock significant capability:

1. **Paged Prefix Sharing** -- oMLX already implements this at the server level. Luxe needs prompt structuring for maximum prefix reuse and metrics to measure it.
2. **Model Context Protocol (MCP)** -- Replace the closed tool system with the emerging industry standard. Luxe becomes both an MCP client (consuming external tools) and MCP server (exposing agents to Claude Desktop, Cursor, etc.).
3. **MLX-JANG** -- Mixed-bit quantization that assigns different bit widths per tensor sensitivity tier. Dramatic quality gains on MoE models (Qwen3.5-122B JANG_2S = 79% MMLU vs MLX 2-bit = 56.5%). oMLX and vMLX already load JANG models natively.

---

## Phase 1: MLX-JANG Integration (lowest risk, highest immediate value)

JANG is purely additive config and metadata. oMLX/vMLX handle loading; luxe needs detection, configuration, and benchmarking.

### 1.1 New module: `luxe/jang/`

**`luxe/jang/__init__.py`** -- Module init, feature detection:
```python
try:
    import jang_tools
    JANG_AVAILABLE = True
except ImportError:
    JANG_AVAILABLE = False
```

**`luxe/jang/detect.py`** -- Model detection and metadata parsing:
- `is_jang_model(model_path: Path) -> bool` -- checks for `jang_config.json` (or legacy `jjqf_config.json`)
- `parse_jang_config(model_path: Path) -> JANGConfig | None` -- extracts profile, avg_bits, tier distribution
- `detect_jang_models(models_dir: Path) -> dict[str, JANGConfig]` -- scans directory for all JANG models

```python
@dataclass
class JANGConfig:
    base_model: str
    profile: str          # "JANG_2S", "JANG_2L", "JANG_4K", etc.
    avg_bits: float
    total_size_gb: float
    bit_widths_used: list[int]
```

**`luxe/jang/profiles.py`** -- Known JANG profile metadata for config validation.

### 1.2 Config changes

**`luxe/configs/candidates.yaml`** -- Add JANG model candidates:
```yaml
- family: qwen2.5-coder
  name: "Qwen2.5-Coder-14B-JANG_4K"
  repo: "JANGQ-AI/Qwen2.5-Coder-14B-Instruct-JANG_4K"
  params: 14B
  ctx: 32768
  quantization: "jang-4k"
  jang_profile: "JANG_4K"
  avg_bits: 4.0
  status: active

- family: qwen3.5-moe
  name: "Qwen3.5-MoE-A22B-JANG_2L"
  repo: "JANGQ-AI/Qwen3.5-MoE-A22B-Instruct-JANG_2L"
  params: 122B
  active_params: 22B
  ctx: 131072
  quantization: "jang-2l"
  jang_profile: "JANG_2L"
  avg_bits: 2.3
  status: inactive  # pending benchmark
```

**`luxe/configs/agents.yaml`** -- Add JANG model alternatives per agent:
```yaml
code:
  model: "Qwen2.5-Coder-14B-4bit"
  jang_model: "Qwen2.5-Coder-14B-JANG_4K"  # preferred when available
```

### 1.3 Backend/provider updates

**`luxe/luxe_cli/providers/omlx.py`** -- Add `model_info()` query to check if loaded model reports JANG metadata. Informational only; oMLX handles JANG loading transparently.

**`luxe/luxe_cli/backend.py`** -- Add `quantization_type` field to model metadata cache. When selecting a model, prefer JANG variant if:
- JANG model is available on the server
- Config has `prefer_jang: true` (default when JANG deps are installed)
- Model size fits within memory budget

### 1.4 Harness updates

**`luxe/harness/registry.py`** -- Extend candidate registration to include JANG metadata (profile, avg_bits). Comparison reports show quantization type alongside model name.

**`luxe/harness/report.py`** -- Phase A/B/D reports include quantization column.

### 1.5 CLI updates

**`luxe/luxe_cli/main.py`** -- Add `luxe jang-scan [models_dir]` command to discover JANG models. Update `luxe agents` display to show JANG status per agent.

### 1.6 Dependencies

```toml
# pyproject.toml
[project.optional-dependencies]
jang = ["jang[mlx]>=2.0"]
```

### 1.7 Tests

- `tests/test_jang_detect.py` -- Mock filesystem with/without `jang_config.json`, verify detection
- `tests/test_jang_config.py` -- JANG candidate entries in candidates.yaml parse correctly
- `tests/test_jang_preference.py` -- Model selection prefers JANG when available

---

## Phase 2: Paged Prefix Sharing (medium risk, directly measurable)

oMLX already implements paged KV cache + prefix sharing + CoW + SSD persistence. Luxe's job is to **structure prompts** for maximum reuse and **observe cache performance**.

### 2.1 Prompt structure optimization

**Key insight**: oMLX caches KV blocks by content hash. Two requests sharing an identical token prefix share physical KV pages. Currently, luxe agents using the same model have identical system prompts (good) but different first user messages (breaks prefix after system prompt).

**`luxe/luxe_cli/agents/base.py`** -- Restructure message assembly:
```
BEFORE: [system_prompt] [task_prompt_with_context]
AFTER:  [system_prompt] [shared_context_block] [variable_task_prompt]
```

The `shared_context_block` is a second system-role message containing:
- Repo summary (language, LOC, file count)
- Session metadata (task type, constraints)
- Any context that's identical across agent invocations for the same session

This maximizes the prefix that oMLX can cache -- on the second+ agent call with the same model in a session, all shared context is a cache hit.

**`luxe/luxe_cli/runner.py`** -- Compute `shared_context` once at session start, pass to all agent invocations.

### 2.2 Backend cache telemetry

**`luxe/luxe_cli/backend.py`** -- Parse `usage.prompt_tokens_details.cached_tokens` from oMLX responses (OpenAI-compatible field). Add to timing:

```python
@dataclass
class GenerationTiming:
    prompt_tokens: int
    completion_tokens: int
    wall_s: float
    prefix_cache_tokens: int = 0  # NEW

    @property
    def prefix_cache_hit_rate(self) -> float:
        return self.prefix_cache_tokens / self.prompt_tokens if self.prompt_tokens > 0 else 0.0
```

### 2.3 Configuration

**`luxe/configs/optimization_configs.yaml`** -- Update `prompt_cache` variant with explicit settings:
```yaml
prompt_cache:
  prefix_sharing: true
  ssd_persist: true
  shared_context_mode: auto    # auto | fixed | none
  # auto: system prompt + repo summary shared
  # fixed: only system prompt shared
  # none: no prefix optimization
```

### 2.4 Metrics and reporting

**`luxe/harness/metrics.py`** -- Add prefix cache columns to evaluation metrics.

**`luxe/luxe_cli/session.py`** -- Log prefix cache stats per turn in JSONL events.

**Harness Phase D** -- Add prefix cache efficiency as a metric alongside TTFT and throughput. Compare TTFT with/without prefix sharing to measure actual speedup.

### 2.5 Tests

- `test_prefix_prompt_structure.py` -- Verify that two workers with same model produce byte-identical prefix up to the variable task portion
- `test_prefix_cache_parsing.py` -- Mock oMLX response with `cached_tokens` field, verify parsing
- `test_prefix_metrics.py` -- Verify metrics collection and reporting
- Integration: Send identical prefix twice to live oMLX, confirm `cached_tokens > 0` on second request

---

## Phase 3: MCP Integration (highest complexity, most new code)

### 3.1 Architecture decisions

**Luxe as MCP Client**: Connect to external MCP servers, discover their tools, inject into agent tool surfaces alongside native tools. This replaces ad-hoc tool additions with a standard protocol.

**Luxe as MCP Server**: Expose luxe agents (code, review, research, etc.) as MCP tools. External clients (Claude Desktop, Cursor, VS Code) invoke luxe agents via MCP.

**Sync/async bridge**: Luxe's codebase is synchronous. MCP SDK is async. Use a dedicated background event loop thread in `MCPClientManager`, accessed via sync wrappers. The `luxe serve` command runs the async MCP server directly.

**Tool namespace**: MCP tools are namespaced as `mcp__{server_name}__{tool_name}` to avoid collision with native tools.

### 3.2 New module: `luxe/luxe_cli/mcp/`

**`luxe/luxe_cli/mcp/__init__.py`** -- Feature detection:
```python
try:
    from mcp import ClientSession
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
```

**`luxe/luxe_cli/mcp/bridge.py`** -- Translation between MCP and luxe tool abstractions:
- `mcp_tool_to_tooldef(tool: mcp.types.Tool, server_name: str) -> ToolDef`
- `tooldef_to_mcp_tool(tooldef: ToolDef) -> mcp.types.Tool`
- `wrap_mcp_call(session: ClientSession, tool_name: str) -> ToolFn` -- wraps async `call_tool` as sync `ToolFn`
- `wrap_native_tool(fn: ToolFn) -> Callable` -- wraps native `ToolFn` as MCP tool handler

The schema translation is trivial -- both use JSON Schema for parameters.

**`luxe/luxe_cli/mcp/client.py`** -- `MCPClientManager`:
```python
class MCPClientManager:
    def __init__(self, server_configs: list[MCPServerConfig]):
        ...
    def connect_all(self) -> None:
        """Initialize transports and connect to each server."""
    def discover_tools(self) -> list[tuple[ToolDef, ToolFn]]:
        """List all tools from all connected servers."""
    def close_all(self) -> None:
        """Cleanup connections."""
```

Supports stdio transport (launches subprocess) and Streamable HTTP transport (connects to URL). Each server gets its own `ClientSession`.

**`luxe/luxe_cli/mcp/server.py`** -- FastMCP-based server exposing luxe agents:
```python
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("luxe")

@mcp.tool()
def luxe_review(repo_path: str, goal: str) -> str:
    """Run luxe code review on a repository."""

@mcp.tool()
def luxe_code(repo_path: str, goal: str) -> str:
    """Run luxe code agent on a repository."""

@mcp.tool()
def luxe_research(query: str) -> str:
    """Run luxe research agent."""
```

Also exposes native filesystem/git tools as MCP tools for external consumption.

### 3.3 Config changes

**`luxe/configs/agents.yaml`** -- Add MCP section:
```yaml
mcp:
  enabled: false
  servers:
    - name: filesystem
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/path"]
    - name: github
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-github"]
      env:
        GITHUB_TOKEN: "${GITHUB_TOKEN}"
  expose_as_server: false
  server_transport: stdio  # or "streamable_http"
```

### 3.4 Tool injection

**`luxe/luxe_cli/agents/base.py`** -- Update `run_agent()` to accept optional `extra_tools`:
```python
def run_agent(
    ...,
    extra_tool_defs: list[ToolDef] | None = None,
    extra_tool_fns: dict[str, ToolFn] | None = None,
) -> AgentResult:
```

Extra tools merge with native tools. MCP tools are NOT added to the cacheable set (external tools may be stateful).

**`luxe/luxe_cli/runner.py`** -- Before dispatching to an agent:
1. If MCP is enabled, get discovered tools from `MCPClientManager`
2. Filter tools per-agent based on config (some agents may only see specific MCP servers)
3. Pass as `extra_tool_defs` / `extra_tool_fns`

### 3.5 CLI changes

**`luxe/luxe_cli/main.py`** -- Add commands:
```
luxe serve [--transport stdio|sse] [--port 8080]   # Start luxe as MCP server
luxe mcp-tools                                       # List discovered MCP tools
```

### 3.6 Dependencies

```toml
[project.optional-dependencies]
mcp = ["mcp>=1.0"]
```

### 3.7 Tests

- `tests/test_mcp_bridge.py` -- Round-trip ToolDef <-> MCP Tool translation
- `tests/test_mcp_injection.py` -- Agent receives merged native + MCP tools
- `tests/test_mcp_config.py` -- MCP config parsing and validation
- `tests/test_mcp_server.py` -- Native tools register correctly as MCP tools
- Integration: Connect to `@modelcontextprotocol/server-filesystem` via stdio, discover tools, call `read_file`

---

## Implementation Order Summary

| Phase | Feature | Risk | Files Modified | New Files | Why This Order |
|-------|---------|------|---------------|-----------|---------------|
| 1 | MLX-JANG | Low | 6 | 4 | Pure config, no runtime changes. Immediate quality gains if JANG models benchmark better. |
| 2 | Prefix Sharing | Medium | 7 | 0 | Touches agent loop and backend but changes are observational + structural. JANG models available for comparison. |
| 3 | MCP | High | 5 | 4 | Most new code, external deps, async bridge. Stable foundation from phases 1-2. |

---

## Key Files to Modify (luxe repo)

| File | Phase | Changes |
|------|-------|---------|
| `luxe/luxe_cli/backend.py` | 1, 2 | JANG model preference, prefix cache telemetry |
| `luxe/luxe_cli/providers/omlx.py` | 1 | `model_info()` for JANG detection via API |
| `luxe/luxe_cli/agents/base.py` | 2, 3 | Shared context block, extra_tools parameter |
| `luxe/luxe_cli/runner.py` | 2, 3 | Shared context computation, MCP tool injection |
| `luxe/luxe_cli/main.py` | 1, 3 | `jang-scan`, `serve`, `mcp-tools` commands |
| `luxe/luxe_cli/session.py` | 2 | Prefix cache stats in JSONL events |
| `luxe/configs/candidates.yaml` | 1 | JANG model entries |
| `luxe/configs/agents.yaml` | 1, 3 | `jang_model` overrides, MCP config section |
| `luxe/configs/optimization_configs.yaml` | 2 | Prefix sharing settings |
| `luxe/harness/registry.py` | 1 | JANG metadata in candidate registration |
| `luxe/harness/report.py` | 1, 2 | Quantization column, prefix cache metrics |
| `pyproject.toml` | 1, 3 | Optional deps: `jang[mlx]`, `mcp` |

## New Files

| File | Phase |
|------|-------|
| `luxe/jang/__init__.py` | 1 |
| `luxe/jang/detect.py` | 1 |
| `luxe/jang/profiles.py` | 1 |
| `luxe/luxe_cli/mcp/__init__.py` | 3 |
| `luxe/luxe_cli/mcp/bridge.py` | 3 |
| `luxe/luxe_cli/mcp/client.py` | 3 |
| `luxe/luxe_cli/mcp/server.py` | 3 |
| `configs/qwen_jang_64gb.yaml` | 1 |

---

## Key Architectural Decisions

### 1. Backward-Compatible Model Spec
`models` in YAML accepts both `str` (existing) and `ModelSpec` object (new). String values auto-promote to `ModelSpec(id=value, quantization="mlx-4bit")`. Existing configs work unchanged.

### 2. MCP Tools as External, Not First-Class
MCP tools inject into agent tool surfaces but are NOT cached. External tool results may be stateful or non-deterministic. Native tools continue to use `ToolCache`.

### 3. Sync Wrapper for Async MCP
Dedicated background event loop thread for MCP async operations. Avoids converting entire codebase to async. `luxe serve` runs the async MCP server directly as top-level entry point.

### 4. Prefix Cache as Observational, Not Prescriptive
Luxe observes and reports prefix cache performance but does not control oMLX's cache eviction or block allocation. oMLX owns the KV cache; luxe structures prompts and measures results.

### 5. JANG Detection via Filesystem + API
Primary: scan model directories for `jang_config.json`. Secondary: query oMLX `/v1/models` for metadata. Both paths work; filesystem is richer, API works when model directory is unknown.

### 6. MCP Server as Separate Command
`luxe serve` starts a dedicated MCP server process. Pipeline runs are one-shot; the MCP server is long-lived. Clean separation of concerns.

---

## Verification Plan

### Phase 1 (JANG)
1. Run `luxe jang-scan ~/.omlx/models/` -- should list any JANG models present
2. Update agents.yaml with a JANG model for one agent (e.g., code)
3. Run `luxe agents` -- should show JANG status
4. Run harness Phase A with both standard and JANG candidate -- compare MMLU/HumanEval+
5. Run existing test suite -- all pass (backward compatible)

### Phase 2 (Prefix Sharing)
1. Run luxe with a review task. Check session JSONL for `prefix_cache_tokens` fields
2. On second+ agent call with same model in a session, `cached_tokens > 0`
3. Run harness Phase D with `prompt_cache` optimization variant -- TTFT should drop significantly
4. Run `luxe analyze` on a repo -- check prefix cache hit rate in output

### Phase 3 (MCP)
1. Run `luxe serve` -- should start MCP server on stdio
2. From a separate terminal, connect with `mcp` CLI and list tools -- should see luxe agents
3. Enable an MCP server in config (e.g., filesystem). Run `luxe mcp-tools` -- should show discovered tools
4. Run a task that triggers an MCP tool -- verify tool result flows through agent loop
5. Configure Claude Desktop to use luxe as MCP server -- invoke `luxe_review` from Claude Desktop

---

## Background Research Summary

### Paged Prefix Sharing
- Combines PagedAttention (fixed-size KV cache blocks in non-contiguous memory) with prefix sharing (identical token prefixes share physical KV pages)
- Two approaches: hash-based block identification (vLLM APC) and radix tree lookup (SGLang RadixAttention)
- oMLX implements paged KV cache + prefix sharing + CoW + SSD-tier persistence
- TTFT drops from 30-90s to <5s on cached prefixes for agentic workloads
- Key data structures: block table (logical->physical mapping), free block queue (LRU), content hash map
- Copy-on-Write for shared blocks when one request diverges

### Model Context Protocol (MCP)
- Anthropic's open standard (JSON-RPC 2.0), donated to Linux Foundation AAIF (Dec 2025)
- Host-client-server architecture with three primitives: Tools, Resources, Prompts
- Transports: stdio (local subprocess), Streamable HTTP (remote), HTTP+SSE (legacy)
- Python SDK: `pip install mcp` with FastMCP high-level API and low-level Server API
- Current spec version: 2025-11-25 (adds Tasks, tool calling in sampling, parallel tool calls)
- Tool schemas use JSON Schema -- same as OpenAI function-calling format, trivial to translate

### MLX-JANG (Mixed-Bit Quantization)
- JANG = Jang Adaptive N-bit Grading, by Jinho Jang (jjang-ai on GitHub)
- Three components: `jangq` (quantizer), `vMLX` (inference server), MLX Studio (macOS app)
- Classifies tensors into CRITICAL (attention, routers: 8-bit), IMPORTANT (embeddings: 4-6 bit), COMPRESS (MLP, experts: 2-4 bit)
- Profiles: JANG_2S, JANG_2L, JANG_3K, JANG_3M, JANG_4K, JANG_4M, JANG_4S
- Dramatic MoE gains: MiniMax M2.5 JANG 2-bit = 74% MMLU vs MLX any bit = ~25%
- JANG v2 models are standard MLX safetensors -- only special handling is per-tensor bit-width fix after loading
- Pre-quantized models on HuggingFace under `JANGQ-AI/` org
- oMLX support via PR #364; vMLX has native support
