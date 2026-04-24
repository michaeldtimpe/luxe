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


class AgentConfig(BaseModel):
    """One specialist agent's configuration."""

    name: AgentName
    display: str
    model: str  # Ollama model tag, e.g. "qwen2.5:7b-instruct"
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
    endpoint: str | None = None  # override top-level ollama_base_url (e.g. llama-server)
    history_keep_last: int = 4  # how many prior session messages to replay on dispatch
    num_ctx: int | None = None  # Ollama num_ctx override — passed as `options.num_ctx`
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
    # Deterministic pre-router: when enabled, a keyword/regex scorer
    # short-circuits the LLM router on decisive prompts. Falls through
    # to the LLM on low-confidence decisions. Disable for A/B testing.
    heuristic_router_enabled: bool = True
    heuristic_router_threshold: float = 0.35
    agents: list[AgentConfig]

    def get(self, name: str) -> AgentConfig:
        for a in self.agents:
            if a.name == name:
                return a
        raise KeyError(f"agent not found: {name}")

    def enabled_specialists(self) -> list[AgentConfig]:
        return [a for a in self.agents if a.enabled and a.name != "router"]


def load_config(path: Path | None = None) -> LuxeConfig:
    path = path or DEFAULT_CONFIG
    with path.open() as f:
        data = yaml.safe_load(f)
    return LuxeConfig(**data)
