"""Pipeline configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class RoleConfig(BaseModel):
    model_key: str
    num_ctx: int = 8192
    max_steps: int = 12
    max_tokens_per_turn: int = 2048
    temperature: float = 0.2
    tools: list[str] = Field(default_factory=list)


class TaskTypeConfig(BaseModel):
    description: str = ""
    pipeline: list[str] = Field(default_factory=list)
    architect_prompt: str = ""


class EscalationConfig(BaseModel):
    worker_read: str = "worker_analyze"
    worker_analyze: str = "worker_code"
    max_retries: int = 1


class ProfileConfig(BaseModel):
    name: str = ""
    description: str = ""
    memory_budget_gb: int = 64
    peak_model_gb: float = 0.0


class PipelineConfig(BaseModel):
    omlx_base_url: str = "http://127.0.0.1:8000"
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    models: dict[str, str] = Field(default_factory=dict)
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
    task_types: dict[str, TaskTypeConfig] = Field(default_factory=dict)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)

    def role(self, name: str) -> RoleConfig:
        if name not in self.roles:
            raise KeyError(f"Unknown pipeline role: {name}")
        return self.roles[name]

    def model_for_role(self, role_name: str) -> str:
        role_cfg = self.role(role_name)
        return self.models[role_cfg.model_key]

    def task_type(self, name: str) -> TaskTypeConfig:
        if name not in self.task_types:
            raise KeyError(f"Unknown task type: {name}. Available: {list(self.task_types)}")
        return self.task_types[name]


def load_config(path: str | Path | None = None) -> PipelineConfig:
    if path is None:
        path = Path(__file__).parent.parent.parent / "configs" / "pipeline.yaml"
    path = Path(path)
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    return PipelineConfig.model_validate(raw)
