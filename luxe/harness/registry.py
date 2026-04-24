"""Candidate + draft model registry loaded from configs/candidates.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"

Family = Literal["qwen", "deepseek", "mistral"]
ToolCallFormat = Literal["hermes", "deepseek", "mistral", "openai-generic"]


class DraftModel(BaseModel):
    id: str
    hf_repo: str
    mlx_repo: str | None = None
    mem_gb_q4: float


class Candidate(BaseModel):
    id: str
    display: str
    active: bool = True
    family: Family
    params_b: float
    dense: bool
    active_params_b: float | None = None
    hf_repo: str
    mlx_repo: str | None = None
    gguf_repo: str | None = None
    gguf_file: str | None = None
    ollama_tag: str | None = None  # tag served by Ollama, e.g. "qwen2.5:7b-instruct"
    chat_template: str
    tool_call_format: ToolCallFormat
    context_native: int
    context_target: int
    mem_gb_q4: float
    draft_id: str | None = None
    notes: str = ""


class OptimizationConfig(BaseModel):
    id: str
    display: str
    weight_quant: str
    kv_quant: Literal["fp16", "q8", "q4"]
    spec_decoding: bool = False
    spec_draft_tokens: int = 0
    prompt_cache: bool = False
    temperature: float = 0.2


class AcceptanceGate(BaseModel):
    max_abs_regression_per_bench: float
    max_abs_regression_tool_call_success: float
    max_b2_task_regression: int
    hard_floor_per_bench: float


class Registry(BaseModel):
    candidates: list[Candidate]
    drafts: list[DraftModel]

    def active_candidates(self) -> list[Candidate]:
        return [c for c in self.candidates if c.active]

    def get(self, candidate_id: str) -> Candidate:
        for c in self.candidates:
            if c.id == candidate_id:
                return c
        raise KeyError(f"candidate not found: {candidate_id}")

    def draft_for(self, candidate: Candidate) -> DraftModel | None:
        if not candidate.draft_id:
            return None
        for d in self.drafts:
            if d.id == candidate.draft_id:
                return d
        raise KeyError(f"draft not found: {candidate.draft_id}")


class OptimizationRegistry(BaseModel):
    configs: list[OptimizationConfig]
    acceptance_gate: AcceptanceGate

    def get(self, config_id: str) -> OptimizationConfig:
        for c in self.configs:
            if c.id == config_id:
                return c
        raise KeyError(f"optimization config not found: {config_id}")


def load_registry(path: Path | None = None) -> Registry:
    path = path or (CONFIG_DIR / "candidates.yaml")
    with path.open() as f:
        data = yaml.safe_load(f)
    return Registry(**data)


def load_optimization_registry(path: Path | None = None) -> OptimizationRegistry:
    path = path or (CONFIG_DIR / "optimization_configs.yaml")
    with path.open() as f:
        data = yaml.safe_load(f)
    return OptimizationRegistry(**data)
