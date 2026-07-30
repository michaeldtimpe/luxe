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


class HostManifest(BaseModel):
    """Per-host model manifest for the interactive front-end (chat-only).

    Keyed by short hostname under `hosts:` in configs/chat.yaml. `main` is the
    model every slot resolves to on this host (unless `slots:` or a CLI/`/model`
    override says otherwise); `fallback` is the model the session auto-degrades
    to — loudly — when `main` cannot be served or fails to load (the 2026-07-29
    deleted-weights incident is the motivating failure). `keep` lists extra
    model ids that must stay on disk (e.g. the benchmark champion on m1) so
    `/doctor` watches them and `luxe pull --remove` refuses them.

    The manifest is a declaration about DISK as well as selection: main,
    fallback, and keep are expected to be locally cached on the host
    (`luxe pull` provisions, `/doctor` verifies). Benchmark/maintain never
    read `hosts:`.
    """

    main: str
    fallback: str = ""
    keep: list[str] = Field(default_factory=list)
    # Per-MODEL `/ctx` ceilings (tokens), keyed by model id. The role-level
    # `num_ctx_max` says what the BOX allows; this says what a specific model
    # on this box allows — KV-cache cost differs wildly per architecture
    # (Qwen3.6 dense = 64 KB/token vs MoE = 20 KB/token, 2026-07-30 audit), so
    # one role ceiling can't be right for both. The effective `/ctx` ceiling
    # is min(role ceiling, this cap for the CURRENT model); absent entry = no
    # extra cap. Applies live: a session degraded to the fallback clamps to
    # the fallback's cap on its next turn.
    ctx_max: dict[str, int] = Field(default_factory=dict)

    def all_models(self) -> list[str]:
        """main + fallback + keep, deduped, order-preserving."""
        out: list[str] = []
        for mid in [self.main, self.fallback, *self.keep]:
            if mid and mid not in out:
                out.append(mid)
        return out


def short_hostname() -> str:
    """Lowercased first label of this machine's hostname ("m1.local" -> "m1").

    Mirrors the normalization chat/origin.py uses for endpoint locality.
    Guarded: returns "" when gethostname fails (it can raise OSError).
    """
    import socket

    try:
        return socket.gethostname().split(".")[0].lower()
    except OSError:
        return ""


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
    # Per-host main/fallback model manifests, keyed by short hostname
    # (chat-only; see HostManifest). Absent block => legacy behaviour
    # (champion-everywhere via the monolith role). NOTE: pydantic drops
    # unknown top-level keys silently, so a typo'd `hosts:` block vanishes
    # without error — `/doctor` and `luxe smoke` assert the resolved manifest
    # to close that hole.
    hosts: dict[str, HostManifest] = Field(default_factory=dict)

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
        This host's manifest models (main/fallback/keep) are always allowed:
        a fallback that isn't rostered would be invisible to `/model` and
        `/doctor` exactly when it matters most.
        """
        if not self.visible_models:
            return list(model_ids)
        allowed = set(self.visible_models)
        manifest = self.host_manifest()
        if manifest is not None:
            allowed.update(manifest.all_models())
        return [m for m in model_ids if m in allowed]

    # -- per-host manifest (chat-only) ---------------------------------------

    def host_manifest(self, hostname: str | None = None) -> HostManifest | None:
        """This host's manifest from `hosts:`, or None (no block / no match).

        Matching is by lowercased short hostname against lowercased keys, so
        `m1.local` matches an `m1:` entry. Chat-only; benchmark/maintain never
        call this.
        """
        if not self.hosts:
            return None
        name = (hostname if hostname is not None else short_hostname())
        if not name:
            return None
        name = name.split(".")[0].lower()
        for key, manifest in self.hosts.items():
            if key.split(".")[0].lower() == name:
                return manifest
        return None

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

        Resolution order for an unconfigured slot (empty `model_key`):
        this host's manifest `main` (when a `hosts:` entry matches), else the
        `monolith` role's model. An explicit `slots:` entry or CLI/`/model`
        override always wins over the manifest. Chat-only — the benchmark/
        maintain path never calls this, so `hosts:` cannot perturb it.
        """
        sc = self.slot_config(slot)
        if sc.model_key:
            return self.models[sc.model_key]
        manifest = self.host_manifest()
        if manifest is not None and manifest.main:
            return manifest.main
        return self.models[self.role("monolith").model_key]

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
