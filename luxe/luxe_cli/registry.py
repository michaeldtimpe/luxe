"""Agent registry — YAML-backed pydantic configs, one per specialist + router.

Mirrors the harness/registry.py pattern. Loaded from configs/agents.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"
DEFAULT_CONFIG = CONFIG_DIR / "agents.yaml"

AgentName = Literal[
    "router", "general", "research", "writing", "image", "code",
    "review", "refactor", "calc", "lookup",
]
ToolName = Literal[
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "list_dir",
    "bash",
    "web_search",
    "fetch_url",
    "fetch_urls",
    "browse_navigate",
    "browse_read",
    "draw_things_generate",
    "create_tool",
    "git_diff",
    "git_log",
    "git_show",
    "lint",
    "typecheck",
    "security_scan",
    "deps_audit",
    "security_taint",
    "secrets_scan",
    "lint_js",
    "typecheck_ts",
    "lint_rust",
    "vet_go",
]


ProviderKind = Literal["ollama", "omlx", "lmstudio", "llamacpp", "mlx"]


class ProviderConfig(BaseModel):
    """Named backend endpoint. Multiple agents can point at the same provider."""

    base_url: str
    kind: ProviderKind


class AgentConfig(BaseModel):
    """One specialist agent's configuration."""

    name: AgentName
    display: str
    model: str  # provider-specific model id (Ollama tag, oMLX tag, etc.)
    system_prompt: str
    tools: list[ToolName] = Field(default_factory=list)
    temperature: float = 0.2
    max_tokens_per_turn: int = 2048
    max_steps: int = 12
    max_wall_s: float = 600.0
    max_tool_calls_per_turn: int = 20
    min_tool_calls: int = 0  # if >0, nudge the agent back into tool use when a final answer arrives below this
    enabled: bool = True
    notes: str = ""
    # Per-agent provider override. References a key in LuxeConfig.providers.
    # When set, takes precedence over `endpoint`. When neither is set, the
    # agent uses LuxeConfig.default_provider (or falls back to ollama_base_url
    # for legacy configs that haven't been migrated).
    provider: str | None = None
    endpoint: str | None = None  # legacy: direct URL override; superseded by `provider`
    history_keep_last: int = 4  # how many prior session messages to replay on dispatch
    num_ctx: int | None = None  # Ollama num_ctx override — passed as `options.num_ctx`
    # Tool output byte cap. Applied to each tool result before it lands
    # in the conversation history. Default 4000 keeps small models honest;
    # bump for code/review/refactor agents where large analyzer payloads
    # lose signal when truncated.
    tool_output_trim_bytes: int = 4000
    # Languages the analyzer tool surface should target. None = no
    # filtering (all 10 analyzers registered). A frozenset of language
    # names (e.g. {"python", "javascript"}) hides analyzers whose
    # language isn't represented — set by the repo-survey step in the
    # /review and /refactor flows to shrink the per-turn tool prompt.
    analyzer_languages: frozenset[str] | None = None


class LuxeConfig(BaseModel):
    """Top-level luxe config."""

    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    session_dir: str = "~/.luxe/sessions"
    draw_things_url: str = "http://127.0.0.1:7860"
    image_output_dir: str = "~/luxe-images"
    # Where /review and /refactor clone target repos. Path is resolved
    # relative to cwd unless absolute or using ~. Default keeps clones
    # out of the project root (and out of git, via .gitignore) instead
    # of dropping them next to the user's source tree.
    local_cache_dir: str = "local-cache"
    # Deterministic pre-router: when enabled, a keyword/regex scorer
    # short-circuits the LLM router on decisive prompts. Falls through
    # to the LLM on low-confidence decisions. Disable for A/B testing.
    heuristic_router_enabled: bool = True
    heuristic_router_threshold: float = 0.35
    # Named provider endpoints. Agents reference these by key via the
    # `provider:` field. Empty by default for backward compatibility:
    # legacy configs that only set ollama_base_url + per-agent `endpoint:`
    # keep working unchanged.
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    # Default provider key used when an agent doesn't specify `provider:`
    # or `endpoint:`. None falls through to ollama_base_url for legacy
    # behavior.
    default_provider: str | None = None
    agents: list[AgentConfig]

    def cache_dir(self) -> Path:
        """Resolve local_cache_dir to an absolute path and ensure it exists.

        Absolute paths and ~/ paths are honored as-given; relative
        paths resolve against the current working directory. The
        directory is created if missing — clones land directly inside.
        """
        p = Path(self.local_cache_dir).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get(self, name: str) -> AgentConfig:
        for a in self.agents:
            if a.name == name:
                return a
        raise KeyError(f"agent not found: {name}")

    def enabled_specialists(self) -> list[AgentConfig]:
        return [a for a in self.agents if a.enabled and a.name != "router"]

    def resolve_endpoint(self, agent: AgentConfig) -> str:
        """Pick the base_url for an agent. Order of precedence:
        1. agent.endpoint (legacy direct URL — explicit wins)
        2. providers[agent.provider].base_url
        3. providers[default_provider].base_url
        4. ollama_base_url (legacy fallback, /v1 suffix stripped)
        """
        if agent.endpoint:
            return agent.endpoint
        key = agent.provider or self.default_provider
        if key and key in self.providers:
            return self.providers[key].base_url
        if key and key not in self.providers:
            raise KeyError(f"provider {key!r} referenced but not declared in providers map")
        # Legacy fallback. Strip /v1 because Backend posts to /v1/chat/completions.
        return self.ollama_base_url.removesuffix("/v1").rstrip("/")


def load_config(path: Path | None = None) -> LuxeConfig:
    path = path or DEFAULT_CONFIG
    with path.open() as f:
        data = yaml.safe_load(f)
    return LuxeConfig(**data)
