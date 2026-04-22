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

AgentName = Literal["router", "general", "research", "writing", "image", "code"]
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
    "draw_things_generate",
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
    enabled: bool = True
    notes: str = ""


class LuxeConfig(BaseModel):
    """Top-level luxe config."""

    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    session_dir: str = "~/.luxe/sessions"
    draw_things_url: str = "http://127.0.0.1:7860"
    image_output_dir: str = "~/luxe-images"
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
