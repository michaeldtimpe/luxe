"""Pipeline configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator


ENGINE_OMLX = "omlx"
ENGINE_LLAMA_SERVER = "llama-server"
#: Cloud, metered, OpenAI-compatible (2026-08-17). The sanctioned chat-only
#: carve-out from luxe.sdd's "no cloud provider backends": opt-in per session,
#: hard-capped by `budget_usd`, never a bench target. See chat.sdd.
ENGINE_OPENROUTER = "openrouter"
#: Serving stacks luxe knows how to talk to. Every one of them speaks the
#: OpenAI-compatible `/v1` surface — which is all the CHAT PATH needs — so this
#: only ever changes DIAGNOSTICS, provisioning, and (openrouter only) the
#: DECLARED `body_extras` merge; never how a turn is otherwise sent.
KNOWN_ENGINES = (ENGINE_OMLX, ENGINE_LLAMA_SERVER, ENGINE_OPENROUTER)


#: `/reasoning` settings. "" is "say nothing and let the provider decide",
#: which is distinct from "off" (ask it not to return reasoning at all).
REASONING_EFFORTS = ("low", "medium", "high")
REASONING_SETTINGS = REASONING_EFFORTS + ("off", "default")


def reasoning_extras(setting: str) -> dict[str, Any] | None:
    """The top-level `reasoning` body field for a `/reasoning` setting.

    None means "send nothing" — both for `default` and for anything
    unrecognised, because a body field a provider rejects is worse than the
    provider's own default. `off` sends `{"exclude": true}`: the model may
    still think, but the channel is not returned (and not billed back to the
    user as visible output).
    """
    norm = (setting or "").strip().lower()
    if norm in REASONING_EFFORTS:
        return {"effort": norm}
    if norm == "off":
        return {"exclude": True}
    return None


class BackendEntry(BaseModel):
    """One named model endpoint for the interactive `luxe chat` front-end.

    Chat-only (like `slots:`): the benchmark/maintain path keeps reading
    `omlx_base_url` and never consults `backends:`. API keys are NEVER stored
    in YAML — `api_key_env` names the environment variable holding the key,
    resolved at Backend construction time (Backend falls back to OMLX_API_KEY
    when the variable is empty/unset). `timeout_s` exists because remote
    endpoints (e.g. m5 over Tailscale running dense 16384-tok turns, ~25 min)
    need a far larger read timeout than the local default.

    `stall_timeout_s` / `decode_stall_timeout_s` are the PROGRESS deadlines
    (B6). `timeout_s` above is a per-read deadline and cannot bound a request
    that oMLX is keeping alive with keepalive bytes while producing nothing;
    these bound it by time-since-actual-progress instead. Both are `None` by
    default, meaning "whatever `Backend` defaults to" — the numbers live in
    `backend.py` alone so a per-endpoint override here can never silently
    drift from the shipped default.

    `engine` names the SERVING STACK behind the URL (2026-08-13, neo). It
    exists because several of luxe's *diagnostics* are oMLX-specific and lie
    against anything else: neo (8 GB, A18 Pro) serves a GGUF through
    llama.cpp's `llama-server`, which needs no API key, exposes no model-path
    catalog, and has no `brew services` unit or admin download API. Declaring
    the engine lets `/doctor` / `luxe ready` skip or reword those checks and
    lets `luxe pull` refuse cleanly, instead of emitting warnings whose fixes
    do not apply. It does NOT change the request luxe sends: every supported
    engine is OpenAI-compatible, and the chat/benchmark request bodies are
    byte-identical across them. Default `omlx` — an entry that does not
    mention `engine` behaves exactly as it did before this field existed.
    """

    base_url: str
    engine: str = ENGINE_OMLX
    api_key_env: str = "OMLX_API_KEY"
    timeout_s: float = 600.0
    stall_timeout_s: float | None = None
    decode_stall_timeout_s: float | None = None
    default: bool = False
    # Whether this endpoint is shared with other clients (B5). luxe's
    # single-residency policy evicts every other model on the server — correct
    # when luxe owns the box, actively harmful on a fleet endpoint like m5,
    # where it pulls weights out from under another host mid-turn. `None` =
    # auto-detect: loopback is owned, anything reachable over the network is
    # assumed shared. Set explicitly to override either way.
    shared: bool | None = None
    # PER-BACKEND model roster (chat-only). Wins over PipelineConfig's global
    # `visible_models:` for this endpoint. It exists for the cloud engine: the
    # global roster is a list of local MLX ids, and applying it to OpenRouter's
    # ~300-model catalog hides all of it. Empty = fall back to the global
    # roster, i.e. exactly the pre-2026-08-17 behaviour for every local entry.
    visible_models: list[str] = Field(default_factory=list)
    # The model every UNPINNED slot resolves to while this endpoint is active
    # (chat-only). Exists because slot defaults come from the HOST manifest —
    # this machine's local weight ids — which an endpoint serving somebody
    # else's catalog does not have: a session that opened on it would sit
    # pointed at a model the server has never heard of until the user typed
    # `/model all <id>`. This is CONFIG-driven selection (the entry names the
    # model), not engine-driven: two entries on the same engine may declare
    # different ones, and an entry that omits it selects exactly as before.
    # It never outranks a user's own choice — see chat/slots.py.
    default_model: str = ""
    # Default reasoning effort for this endpoint (chat-only): "low" | "medium"
    # | "high" | "off" | "" (send nothing, i.e. the provider's own default).
    # A THINKING model bills every reasoning token: a one-sentence question
    # measured 255 characters of answer against 3,568 of reasoning and 792
    # billed completion tokens (2026-08-17). For interactive chat that is a
    # tax on latency and on the bill, so the shipped cloud entry asks for
    # "low" and `/reasoning high` buys the depth back per session.
    reasoning_effort: str = ""
    # HARD spend cap for a session on this endpoint, in USD (chat-only). Only
    # meaningful on a billable engine. None = no cap AND no cost segment — a
    # local endpoint should not grow a "$0.00" chip. A turn is refused BEFORE
    # dispatch once the session's accumulated cost reaches it; `/usage budget
    # <usd>` raises it for the session. See chat.sdd.
    budget_usd: float | None = None
    # Vendor body fields this endpoint DECLARES it wants, merged at the TOP
    # LEVEL of the chat body (never `extra_body` — see backend.py 2026-08-11).
    # OpenRouter needs `{"usage": {"include": true}}` to report per-request
    # cost. Empty for every other entry, and omitted from `backend_kwargs()`
    # when empty, so their requests stay byte-identical.
    body_extras: dict[str, Any] = Field(default_factory=dict)

    @field_validator("engine")
    @classmethod
    def _known_engine(cls, v: str) -> str:
        """Reject an unknown engine at LOAD time, not at first diagnostic.

        A typo here would silently restore the oMLX assumptions this field
        exists to switch off — and it would do it on the fallback host, in an
        outage, where nobody is reading warnings. Fail loudly instead.
        """
        norm = (v or "").strip().lower()
        if norm not in KNOWN_ENGINES:
            raise ValueError(
                f"unknown engine {v!r}; expected one of {', '.join(KNOWN_ENGINES)}")
        return norm

    def is_omlx(self) -> bool:
        """True when this endpoint is oMLX — i.e. when the oMLX-only
        diagnostics (`/v1/models/status` weight paths, the stale-Cellar probe,
        `brew services`, the `/admin/api/*` downloader) mean anything."""
        return self.engine == ENGINE_OMLX

    def is_openrouter(self) -> bool:
        """True for the cloud carve-out: metered, provider-hosted weights, no
        local process to start and nothing to `luxe pull`."""
        return self.engine == ENGINE_OPENROUTER

    def is_billable(self) -> bool:
        """True when tokens on this endpoint cost money. Drives whether the
        cost segment/footer/cap exist at all — a local endpoint must not grow
        a permanent "$0.00" chip."""
        return self.is_openrouter()

    def engine_label(self) -> str:
        """How to name this endpoint in a check line."""
        if self.is_omlx():
            return "oMLX"
        if self.is_openrouter():
            return "OpenRouter"
        return self.engine

    def needs_api_key(self) -> bool:
        """Whether a missing key is worth reporting. llama-server is keyless
        unless started with `--api-key`, so warning about it there is a
        permanent false alarm with a fix that does nothing. On OpenRouter the
        key is not cosmetic at all: without it EVERY request 401s, which is
        why `/doctor` treats a missing one as a FAIL there (chat.sdd)."""
        return self.is_omlx() or self.is_openrouter()

    def is_shared(self) -> bool:
        """True when other clients may be using this endpoint.

        Auto-detection is deliberately conservative: the cost of wrongly
        assuming *shared* is some extra RAM held on a machine luxe owns; the
        cost of wrongly assuming *owned* is evicting a colleague's model
        mid-generation. Default to the cheap mistake.
        """
        if self.shared is not None:
            return self.shared
        host = urlparse(self.base_url).hostname or ""
        return host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "")

    def backend_kwargs(self) -> dict[str, Any]:
        """Constructor kwargs for `Backend(...)`, omitting anything unset.

        Omission matters: passing `None` through would override Backend's
        defaults with nothing. An entry that doesn't mention the progress
        deadlines — or `body_extras` — must behave exactly as it did before
        they existed, which is what keeps every local endpoint's request
        byte-identical (tests/test_golden_request.py).
        """
        kw: dict[str, Any] = {"timeout_s": self.timeout_s}
        if self.stall_timeout_s is not None:
            kw["stall_timeout_s"] = self.stall_timeout_s
        if self.decode_stall_timeout_s is not None:
            kw["decode_stall_timeout_s"] = self.decode_stall_timeout_s
        extras = dict(self.body_extras)
        # `reasoning_effort:` is sugar over the same declared-body mechanism —
        # spelled as its own key because it is the one vendor field a user is
        # expected to set, and `/reasoning` rewrites it live on the Backend
        # this builds. Only meaningful where the provider reads it, so it is
        # confined to the cloud engine rather than posted at oMLX.
        if self.is_openrouter():
            reasoning = reasoning_extras(self.reasoning_effort)
            if reasoning is not None:
                extras["reasoning"] = reasoning
        if extras:
            kw["body_extras"] = extras
        if self.is_openrouter():
            # `num_ctx` is an Ollama/oMLX-ism. A hosted provider fixes the
            # window per model and ignores the field, so sending it is noise
            # on the wire; luxe's own num_ctx stays meaningful client-side
            # (compaction thresholds, ctx%). Omitted for every other entry, so
            # nothing that has ever sent it stops.
            kw["send_num_ctx"] = False
        if self.is_billable():
            # A third-party endpoint must never inherit the fleet's oMLX key
            # because its own `api_key_env` happened to be unset — that would
            # put a local credential in an Authorization header pointed at
            # someone else's server. Omitted (not passed as True) for every
            # local entry so their Backend construction is untouched.
            kw["key_fallback"] = False
        return kw


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
    # Chat-only: distil a few bullets of session working notes into
    # `<repo>/.luxe/memory.md` when a session with a project ends
    # (chat/notes.py). `/note` works regardless — an explicit invocation is
    # consent. Benchmark/maintain never read it.
    notes: bool = True

    def role(self, name: str) -> RoleConfig:
        if name not in self.roles:
            raise KeyError(f"Unknown pipeline role: {name}")
        return self.roles[name]

    def model_for_role(self, role_name: str) -> str:
        role_cfg = self.role(role_name)
        return self.models[role_cfg.model_key]

    def visible(self, model_ids: list[str], *,
                entry: "BackendEntry | None" = None) -> list[str]:
        """Filter server-reported model ids to the configured roster.

        `entry` is the ACTIVE backend entry. When it declares its own
        `visible_models`, that roster wins outright: the global list names
        local MLX weights, and intersecting it with a cloud catalog of ~300
        third-party ids leaves nothing selectable. Omitting `entry` (every
        pre-2026-08-17 caller) keeps the global-roster behaviour exactly.

        Server order is preserved so `/model <slot> <n>` indexes stay stable.
        An id in `visible_models` that the server does NOT serve is dropped
        silently — the roster is a filter, not a claim about what exists.
        This host's manifest models (main/fallback/keep) are always allowed:
        a fallback that isn't rostered would be invisible to `/model` and
        `/doctor` exactly when it matters most.
        """
        if entry is not None and entry.visible_models:
            allowed_entry = set(entry.visible_models)
            return [m for m in model_ids if m in allowed_entry]
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

    def model_for_slot(self, slot: str, *, manifest_host: str | None = None) -> str:
        """Resolve a chat slot to a concrete model id.

        Resolution order for an unconfigured slot (empty `model_key`):
        the manifest `main` (when a `hosts:` entry matches), else the
        `monolith` role's model. An explicit `slots:` entry or CLI/`/model`
        override always wins over the manifest. Chat-only — the benchmark/
        maintain path never calls this, so `hosts:` cannot perturb it.

        `manifest_host` picks WHOSE manifest resolves the default: None (the
        chat-session rule, chat.sdd — this host's) or an endpoint host (the
        drill rule — `luxe ready --backend m5` judges m5's pair, not ours).
        """
        sc = self.slot_config(slot)
        if sc.model_key:
            return self.models[sc.model_key]
        manifest = self.host_manifest(manifest_host)
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
