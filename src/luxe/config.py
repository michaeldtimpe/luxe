"""Pipeline configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class BackendEntry(BaseModel):
    """One named oMLX endpoint for the interactive `luxe chat` front-end.

    Chat-only (like `slots:`): the benchmark/maintain path keeps reading
    `omlx_base_url` and never consults `backends:`. API keys are NEVER stored
    in YAML — `api_key_env` names the environment variable holding the key,
    resolved at Backend construction time (Backend falls back to OMLX_API_KEY
    when the variable is empty/unset). `timeout_s` exists because remote
    endpoints (e.g. m5 over Tailscale running dense 16384-tok turns, ~25 min)
    need a far larger read timeout than the local default.
    """

    base_url: str
    api_key_env: str = "OMLX_API_KEY"
    timeout_s: float = 600.0
    default: bool = False


class RoleConfig(BaseModel):
    model_key: str
    num_ctx: int = 8192
    # Hard ceiling for the interactive `luxe chat` /ctx size flag. 0 (default)
    # means "no expansion" — /ctx can never raise num_ctx above `num_ctx`, so a
    # box that hasn't opted in stays exactly where it is. Set per-machine to the
    # largest window the model + RAM can hold (e.g. 131072 on a 64 GB M-series).
    # Chat-only: the benchmark/maintain path never reads it.
    num_ctx_max: int = 0
    max_steps: int = 12
    max_tokens_per_turn: int = 2048
    temperature: float = 0.2
    tools: list[str] = Field(default_factory=list)
    # Prompt-shaping bake-off levers (default to baseline-equivalent).
    # See src/luxe/agents/prompts.py for the registry.
    system_prompt_id: str = "baseline"
    task_prompt_id: str = "baseline"
    # Per-task-type overlay (Branch B). Empty string = no overlay; use
    # the role-level prompt ids above for every task type. When set,
    # the overlay's by_task mapping wins for matching task types.
    # See ~/.claude/plans/task-type-overlays.md.
    task_overlay_id: str = ""
    # Sampling penalty forwarded as oMLX extra_body. None = omit (current
    # behaviour). Small values (1.02-1.10) discourage repeated tokens; too
    # aggressive corrupts code-gen by forcing identifier divergence.
    repeat_penalty: float | None = None


class SlotConfig(BaseModel):
    """A single model slot for the interactive `luxe chat` front-end.

    `model_key` indexes `PipelineConfig.models`; an empty string falls back to
    the `monolith` role's model_key (the champion). `role` selects which
    `RoleConfig` drives `run_single` for turns routed to this slot.
    """

    model_key: str = ""
    role: str = "monolith"


class ChatSlots(BaseModel):
    """Per-work-type model slots for `luxe chat` (opt-in fan-out).

    Default-constructed slots have empty `model_key`, so `model_for_slot`
    resolves every slot to the champion — byte-identical model selection to a
    config with no `slots:` block at all. See luxe.sdd for the sanctioned-exception
    contract.
    """

    chat: SlotConfig = Field(default_factory=SlotConfig)
    plan: SlotConfig = Field(default_factory=SlotConfig)
    code: SlotConfig = Field(default_factory=SlotConfig)


class TaskTypeConfig(BaseModel):
    description: str = ""
    pipeline: list[str] = Field(default_factory=list)
    architect_prompt: str = ""


class ProfileConfig(BaseModel):
    name: str = ""
    description: str = ""
    memory_budget_gb: int = 64
    peak_model_gb: float = 0.0


class PipelineConfig(BaseModel):
    omlx_base_url: str = "http://127.0.0.1:8000"
    # Named oMLX endpoints for `luxe chat` (`/backend`, `--backend`). Chat-only;
    # when absent, `backend_entries()` synthesizes {"local": omlx_base_url} so
    # every existing config parses (and behaves) identically. The benchmark/
    # maintain path reads omlx_base_url only.
    backends: dict[str, BackendEntry] = Field(default_factory=dict)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    models: dict[str, str] = Field(default_factory=dict)
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
    task_types: dict[str, TaskTypeConfig] = Field(default_factory=dict)
    # Interactive-only model slots (`luxe chat`). None or empty model_keys =>
    # champion-everywhere (no fan-out). Read only by the chat front-end.
    slots: ChatSlots | None = None
    # Chat-only roster filter: when non-empty, `/model` lists ONLY these ids and
    # slots may only resolve to them. The oMLX server can serve a dozen stale
    # models (old bake-off entries, HF-cache aliases); this is the working set.
    # Empty = show everything the server reports (previous behaviour).
    # Benchmark/maintain never read it.
    visible_models: list[str] = Field(default_factory=list)

    def role(self, name: str) -> RoleConfig:
        if name not in self.roles:
            raise KeyError(f"Unknown pipeline role: {name}")
        return self.roles[name]

    def model_for_role(self, role_name: str) -> str:
        role_cfg = self.role(role_name)
        return self.models[role_cfg.model_key]

    def visible(self, model_ids: list[str]) -> list[str]:
        """Filter server-reported model ids to the configured roster.

        Server order is preserved so `/model <slot> <n>` indexes stay stable.
        An id in `visible_models` that the server does NOT serve is dropped
        silently — the roster is a filter, not a claim about what exists.
        """
        if not self.visible_models:
            return list(model_ids)
        allowed = set(self.visible_models)
        return [m for m in model_ids if m in allowed]

    def slot_config(self, slot: str) -> SlotConfig:
        """Return the SlotConfig for `slot`, defaulting to an empty SlotConfig
        (which resolves to the champion) when `slots:` is absent."""
        if slot not in ("chat", "plan", "code"):
            raise KeyError(f"Unknown chat slot: {slot}. Expected chat|plan|code.")
        if self.slots is None:
            return SlotConfig()
        return getattr(self.slots, slot)

    def model_for_slot(self, slot: str) -> str:
        """Resolve a chat slot to a concrete model id.

        Falls back to the `monolith` role's model whenever the slot's
        `model_key` is empty, so an unconfigured slot is the champion — identical
        to `model_for_role("monolith")`.
        """
        sc = self.slot_config(slot)
        key = sc.model_key or self.role("monolith").model_key
        return self.models[key]

    # -- multi-backend (chat-only) -------------------------------------------

    def backend_entries(self) -> dict[str, BackendEntry]:
        """The configured `backends:` map, or a synthesized single "local"
        entry built from `omlx_base_url` when the block is absent — so configs
        that predate multi-backend behave identically."""
        if self.backends:
            return self.backends
        return {"local": BackendEntry(base_url=self.omlx_base_url)}

    def backend_entry(self, name: str) -> BackendEntry:
        entries = self.backend_entries()
        if name not in entries:
            raise KeyError(
                f"Unknown backend: {name!r}. Available: {list(entries)}")
        return entries[name]

    def default_backend_name(self) -> str:
        """The entry flagged `default: true`, else the first entry (insertion
        order — synthesized configs have exactly one, "local")."""
        entries = self.backend_entries()
        for name, entry in entries.items():
            if entry.default:
                return name
        return next(iter(entries))

    def task_type(self, name: str) -> TaskTypeConfig:
        if name not in self.task_types:
            raise KeyError(f"Unknown task type: {name}. Available: {list(self.task_types)}")
        return self.task_types[name]


def load_config(path: str | Path | None = None) -> PipelineConfig:
    if path is None:
        path = Path(__file__).parent.parent.parent / "configs" / "single_64gb.yaml"
    path = Path(path)
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    return PipelineConfig.model_validate(raw)
