"""Tests for config loading and validation."""

from pathlib import Path

import pytest

from luxe.config import (ChatSlots, HostManifest, PipelineConfig, RoleConfig,
                         SlotConfig, load_config)


def test_load_default_config(config_path: Path):
    cfg = load_config(config_path)
    assert cfg.omlx_base_url.startswith("http")
    assert "monolith" in cfg.roles


def test_model_for_role(config_path: Path):
    cfg = load_config(config_path)
    model = cfg.model_for_role("monolith")
    assert model  # non-empty model id


def test_task_types(config_path: Path):
    cfg = load_config(config_path)
    assert "review" in cfg.task_types
    assert "implement" in cfg.task_types
    review = cfg.task_type("review")
    assert "monolith" in review.pipeline


def test_role_configs(config_path: Path):
    cfg = load_config(config_path)
    mono = cfg.role("monolith")
    assert mono.max_steps > 0
    assert "read_file" in mono.tools
    assert "edit_file" in mono.tools


# --- prompt-shaping bake-off RoleConfig extensions --

def test_role_config_prompt_shaping_defaults():
    """system_prompt_id, task_prompt_id, repeat_penalty must default such
    that existing configs (which omit them entirely) load unchanged."""
    rc = RoleConfig(model_key="x")
    assert rc.system_prompt_id == "baseline"
    assert rc.task_prompt_id == "baseline"
    assert rc.repeat_penalty is None


def test_role_config_prompt_shaping_overrides_round_trip():
    """Explicit overrides must round-trip through model_dump/model_validate
    so YAML overlays from the bench harness preserve them."""
    rc = RoleConfig(
        model_key="x",
        system_prompt_id="cot",
        task_prompt_id="cot",
        repeat_penalty=1.05,
        temperature=0.3,
    )
    dumped = rc.model_dump()
    rc2 = RoleConfig.model_validate(dumped)
    assert rc2.system_prompt_id == "cot"
    assert rc2.task_prompt_id == "cot"
    assert rc2.repeat_penalty == 1.05
    assert rc2.temperature == 0.3


def test_existing_yaml_loads_without_new_fields(config_path: Path):
    """The shipped configs/single_64gb.yaml does not list the new fields;
    loading must succeed and use defaults."""
    cfg = load_config(config_path)
    mono = cfg.role("monolith")
    assert mono.system_prompt_id == "baseline"
    assert mono.task_prompt_id == "baseline"
    assert mono.repeat_penalty is None


def test_role_config_task_overlay_id_default():
    """task_overlay_id defaults to empty string (no overlay)."""
    rc = RoleConfig(model_key="x")
    assert rc.task_overlay_id == ""


def test_role_config_task_overlay_id_round_trip(tmp_path: Path):
    """A YAML overlay setting `task_overlay_id: implement_via_cot` must
    parse and round-trip — mirrors what `make_overlay()` writes for
    Branch B variant cells."""
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "omlx_base_url: http://127.0.0.1:8000\n"
        "models: {monolith: Test-Model}\n"
        "roles:\n"
        "  monolith:\n"
        "    model_key: monolith\n"
        "    tools: [read_file]\n"
        "    task_overlay_id: implement_via_cot\n"
        "task_types:\n"
        "  implement: {description: x, pipeline: [monolith]}\n"
    )
    cfg = load_config(overlay)
    mono = cfg.role("monolith")
    assert mono.task_overlay_id == "implement_via_cot"


def test_role_config_repeat_penalty_accepts_float(tmp_path: Path):
    """A YAML overlay setting `repeat_penalty: 1.05` must parse as float
    (mirrors what `make_overlay()` writes for prompt-shaping cells)."""
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "omlx_base_url: http://127.0.0.1:8000\n"
        "models: {monolith: Test-Model}\n"
        "roles:\n"
        "  monolith:\n"
        "    model_key: monolith\n"
        "    tools: [read_file]\n"
        "    repeat_penalty: 1.05\n"
        "    system_prompt_id: cot\n"
        "task_types:\n"
        "  implement: {description: x, pipeline: [monolith]}\n"
    )
    cfg = load_config(overlay)
    mono = cfg.role("monolith")
    assert mono.repeat_penalty == 1.05
    assert mono.system_prompt_id == "cot"


# --- chat model slots (opt-in fan-out, default champion-everywhere) ---

def test_slots_absent_resolves_every_slot_to_champion(config_path: Path):
    """single_64gb.yaml has no `slots:` block — every chat slot must resolve
    to the champion, identical to model_for_role('monolith')."""
    cfg = load_config(config_path)
    assert cfg.slots is None
    champ = cfg.model_for_role("monolith")
    for slot in ("chat", "plan", "code"):
        assert cfg.model_for_slot(slot) == champ


def test_chat_yaml_default_follows_host_manifest(monkeypatch):
    """2026-07-30 fallback-kit pivot: with `slots:` omitted, every slot
    resolves to THIS host's manifest `main`; a host with no `hosts:` entry
    keeps the champion. Benchmark/maintain never read `hosts:` or slots, so
    single_64gb.yaml is untouched (covered by the test above)."""
    import luxe.config as config_mod

    chat_cfg = Path(__file__).parent.parent / "configs" / "chat.yaml"
    cfg = load_config(chat_cfg)
    assert cfg.slots is None
    champ = cfg.model_for_role("monolith")

    # Fleet hosts resolve to their declared mains, uniformly across slots.
    # (m1/m4 main is the MoE — flipped 2026-07-30; dense-27B prefill is ~65
    # tok/s on oMLX's vlm engine, unusable interactively.)
    for host, expected in (("m5", champ), ("m1", "Qwen3.6-35B-A3B-4bit"),
                           ("m4", "Qwen3.6-35B-A3B-4bit")):
        monkeypatch.setattr(config_mod, "short_hostname", lambda h=host: h)
        for slot in ("chat", "plan", "code"):
            assert cfg.model_for_slot(slot) == expected, (host, slot)

    # An unknown host (no manifest entry) falls back to the champion.
    monkeypatch.setattr(config_mod, "short_hostname", lambda: "zeta")
    assert cfg.model_for_slot("chat") == champ


def test_chat_yaml_manifests_declare_fallbacks():
    """Every fleet host's manifest has a non-empty fallback distinct from its
    main (the auto-degrade contract needs both), and m1 keeps the benchmark
    champion on disk (the bench exception)."""
    chat_cfg = Path(__file__).parent.parent / "configs" / "chat.yaml"
    cfg = load_config(chat_cfg)
    assert set(cfg.hosts) == {"m1", "m4", "m5"}
    for name, m in cfg.hosts.items():
        assert m.main and m.fallback and m.fallback != m.main, name
    assert "Qwen3.6-35B-A3B-6bit" in cfg.hosts["m1"].keep
    # Per-model /ctx clamps (2026-07-30 KV audit): dense-27B is capped at the
    # 32K default on the small hosts (64 KB/token KV + ~65 tok/s prefill);
    # the m1 bench champion clamps at 128K (28.4 GiB weights leave ~7.6 GiB).
    assert cfg.hosts["m1"].ctx_max["Qwen3.6-27B-4bit"] == 32768
    assert cfg.hosts["m1"].ctx_max["Qwen3.6-35B-A3B-6bit"] == 131072
    assert cfg.hosts["m4"].ctx_max["Qwen3.6-27B-4bit"] == 32768
    assert "Qwen3.6-35B-A3B-4bit" not in cfg.hosts["m1"].ctx_max  # MoE uncapped


def test_host_manifest_normalizes_hostnames():
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        hosts={"M1": HostManifest(main="A", fallback="B")},
    )
    assert cfg.host_manifest("m1.local").main == "A"
    assert cfg.host_manifest("M1.Tailnet.ts.net").main == "A"
    assert cfg.host_manifest("m5") is None
    assert cfg.host_manifest("") is None


def test_explicit_slot_beats_host_manifest():
    """slots:/CLI overrides win over the manifest — the manifest is a default,
    not a lock."""
    cfg = PipelineConfig(
        models={"monolith": "Champ", "coder": "Coder"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        slots=ChatSlots(code=SlotConfig(model_key="coder")),
        hosts={"here": HostManifest(main="Main-M", fallback="Fb-M")},
    )
    assert cfg.model_for_slot("code") == "Coder"


def test_visible_always_allows_manifest_models(monkeypatch):
    """The roster filter must never hide this host's manifest models — a
    fallback invisible to /model and /doctor fails exactly when needed."""
    import luxe.config as config_mod

    monkeypatch.setattr(config_mod, "short_hostname", lambda: "here")
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        visible_models=["Champ"],
        hosts={"here": HostManifest(main="Main-M", fallback="Fb-M")},
    )
    served = ["Champ", "Main-M", "Fb-M", "Stale-Model"]
    assert cfg.visible(served) == ["Champ", "Main-M", "Fb-M"]


def test_empty_model_key_falls_back_to_champion():
    """An explicit `slots:` block with empty model_keys still resolves to the
    champion — only a non-empty model_key activates fan-out."""
    slots = ChatSlots()  # all default SlotConfig() => model_key=""
    cfg = PipelineConfig(
        models={"monolith": "Champ-Model"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        slots=slots,
    )
    assert cfg.model_for_slot("code") == "Champ-Model"


def test_distinct_slot_model_activates_fanout():
    cfg = PipelineConfig(
        models={"monolith": "Champ-Model", "coder": "Coder-Model"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        slots=ChatSlots(code=SlotConfig(model_key="coder")),
    )
    assert cfg.model_for_slot("chat") == "Champ-Model"
    assert cfg.model_for_slot("code") == "Coder-Model"


def test_slots_round_trip_through_yaml(tmp_path: Path):
    overlay = tmp_path / "chat_overlay.yaml"
    overlay.write_text(
        "omlx_base_url: http://127.0.0.1:8000\n"
        "models: {monolith: Champ, coder: Coder}\n"
        "roles:\n"
        "  monolith:\n"
        "    model_key: monolith\n"
        "    tools: [read_file]\n"
        "task_types:\n"
        "  review: {description: x, pipeline: [monolith]}\n"
        "slots:\n"
        "  code: {model_key: coder}\n"
    )
    cfg = load_config(overlay)
    assert cfg.model_for_slot("code") == "Coder"
    assert cfg.model_for_slot("chat") == "Champ"


def test_unknown_slot_raises():
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
    )
    with pytest.raises(KeyError):
        cfg.model_for_slot("planner")


# --- multi-backend `backends:` map (chat-only; synthesized when absent) ---

def test_backends_absent_synthesizes_local_from_omlx_base_url(config_path: Path):
    """Configs that predate `backends:` must parse identically: the raw field
    stays empty, and backend_entries() synthesizes a single "local" entry
    pointing at omlx_base_url with the stock Backend defaults."""
    cfg = load_config(config_path)
    assert cfg.backends == {}
    entries = cfg.backend_entries()
    assert list(entries) == ["local"]
    entry = entries["local"]
    assert entry.base_url == cfg.omlx_base_url
    assert entry.api_key_env == "OMLX_API_KEY"
    assert entry.timeout_s == 600.0
    assert entry.default is False
    assert cfg.default_backend_name() == "local"
    assert cfg.backend_entry("local").base_url == cfg.omlx_base_url


def test_backend_entry_defaults():
    from luxe.config import BackendEntry
    e = BackendEntry(base_url="http://x:8000")
    assert e.api_key_env == "OMLX_API_KEY"
    assert e.timeout_s == 600.0
    assert e.default is False


def test_backends_parse_from_yaml(tmp_path: Path):
    overlay = tmp_path / "multi.yaml"
    overlay.write_text(
        "omlx_base_url: http://127.0.0.1:8000\n"
        "backends:\n"
        "  local: {base_url: 'http://127.0.0.1:8000', default: true}\n"
        "  m5:\n"
        "    base_url: 'http://m5.example.ts.net:8000'\n"
        "    api_key_env: OMLX_API_KEY_M5\n"
        "    timeout_s: 2400\n"
        "models: {monolith: Champ}\n"
        "roles:\n"
        "  monolith: {model_key: monolith, tools: [read_file]}\n"
        "task_types:\n"
        "  review: {description: x, pipeline: [monolith]}\n"
    )
    cfg = load_config(overlay)
    assert set(cfg.backends) == {"local", "m5"}
    assert cfg.default_backend_name() == "local"
    m5 = cfg.backend_entry("m5")
    assert m5.base_url == "http://m5.example.ts.net:8000"
    assert m5.api_key_env == "OMLX_API_KEY_M5"
    assert m5.timeout_s == 2400.0
    # keys are env-var NAMES only — never key material in YAML
    assert "api_key" not in type(m5).model_fields


def test_backend_entry_unknown_raises():
    cfg = PipelineConfig(models={"monolith": "Champ"},
                         roles={"monolith": RoleConfig(model_key="monolith")})
    with pytest.raises(KeyError):
        cfg.backend_entry("nope")


def test_default_backend_name_falls_back_to_first_entry(tmp_path: Path):
    """No entry flagged default → the first (insertion-order) entry wins."""
    from luxe.config import BackendEntry
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        backends={"a": BackendEntry(base_url="http://a:8000"),
                  "b": BackendEntry(base_url="http://b:8000")},
    )
    assert cfg.default_backend_name() == "a"


def test_chat_yaml_ships_local_default_and_m5():
    chat_cfg = Path(__file__).parent.parent / "configs" / "chat.yaml"
    cfg = load_config(chat_cfg)
    assert cfg.default_backend_name() == "local"
    assert cfg.backend_entry("local").base_url == "http://127.0.0.1:8000"
    m5 = cfg.backend_entry("m5")
    assert m5.base_url == "http://m5.tailca7308.ts.net:8000"
    assert m5.api_key_env == "OMLX_API_KEY_M5"
    assert m5.timeout_s == 2400.0


def test_dense_m5_yaml_defaults_to_m5_backend():
    """The dense-27B-on-m5 config folds the old hardcoded-timeout/tunnel hack
    into the backends scheme: m5 is the default entry with the long timeout."""
    p = Path(__file__).parent.parent / "configs" / "dense_27b_6bit_m5.yaml"
    cfg = load_config(p)
    assert cfg.default_backend_name() == "m5"
    assert cfg.backend_entry("m5").timeout_s == 2400.0
    assert cfg.backend_entry("local").base_url == "http://127.0.0.1:8000"
    # non-chat paths keep reading omlx_base_url → also m5
    assert cfg.omlx_base_url == cfg.backend_entry("m5").base_url


class TestBackendEngine:
    """`backends: <name>: engine:` — which serving stack is behind the URL.

    Added 2026-08-13 for neo, which serves a GGUF through llama.cpp's
    `llama-server`. It steers DIAGNOSTICS only; the request luxe sends is
    identical on every engine.
    """

    def test_defaults_to_omlx_so_existing_configs_are_unchanged(self):
        from luxe.config import ENGINE_OMLX, BackendEntry
        e = BackendEntry(base_url="http://127.0.0.1:8000")
        assert e.engine == ENGINE_OMLX
        assert e.is_omlx() and e.needs_api_key()
        assert e.engine_label() == "oMLX"

    def test_llama_server_switches_the_omlx_only_affordances_off(self):
        from luxe.config import BackendEntry
        e = BackendEntry(base_url="http://127.0.0.1:8080", engine="llama-server")
        assert not e.is_omlx()
        assert not e.needs_api_key()
        assert e.engine_label() == "llama-server"

    def test_case_and_whitespace_are_normalised(self):
        from luxe.config import BackendEntry
        assert BackendEntry(base_url="u", engine="  LLAMA-Server ").engine == \
            "llama-server"

    def test_an_unknown_engine_is_rejected_at_load(self):
        from pydantic import ValidationError

        from luxe.config import BackendEntry
        # A typo must not silently restore the oMLX assumptions — on the
        # fallback host, in an outage, nobody reads the warnings.
        with pytest.raises(ValidationError):
            BackendEntry(base_url="u", engine="llama_server")

    def test_engine_travels_through_a_loaded_yaml(self, tmp_path):
        from luxe.config import load_config
        p = tmp_path / "c.yaml"
        p.write_text(
            "models: {monolith: M}\n"
            "roles: {monolith: {model_key: monolith}}\n"
            "backends:\n"
            "  local: {base_url: 'http://127.0.0.1:8080', "
            "engine: llama-server, default: true}\n")
        cfg = load_config(str(p))
        assert cfg.backend_entry("local").is_omlx() is False

    def test_a_synthesised_entry_is_omlx(self):
        """No `backends:` block ⇒ the synthesized 'local' entry, unchanged."""
        cfg = PipelineConfig(models={"monolith": "M"},
                             roles={"monolith": RoleConfig(model_key="monolith")})
        assert cfg.backend_entries()["local"].is_omlx()

    def test_backend_kwargs_is_unaffected_by_engine(self):
        """Engine must not leak into the wire/timeout surface."""
        from luxe.config import BackendEntry
        a = BackendEntry(base_url="u")
        b = BackendEntry(base_url="u", engine="llama-server")
        assert a.backend_kwargs() == b.backend_kwargs()


class TestOpenRouterEngine:
    """`engine: openrouter` — the CLOUD carve-out (2026-08-17).

    luxe.sdd forbids cloud backends on the benchmark/maintain path and always
    will; this is the one sanctioned, opt-in, billable exception, scoped to
    `luxe chat`. What these pin is the shape that makes it safe: a key that is
    fatal rather than cosmetic, a hard spend cap, a per-backend roster (the
    global one names local MLX weights and would hide the entire catalog), and
    declared body extras that reach the wire without touching anyone else's.
    """

    def test_the_engine_is_known_and_labelled(self):
        from luxe.config import ENGINE_OPENROUTER, BackendEntry
        e = BackendEntry(base_url="https://openrouter.ai/api",
                         engine="openrouter")
        assert e.engine == ENGINE_OPENROUTER
        assert e.is_openrouter() and e.is_billable()
        assert not e.is_omlx()
        assert e.engine_label() == "OpenRouter"

    def test_the_api_key_is_not_optional_there(self):
        """llama-server is keyless; OpenRouter 401s every request without one,
        which is why `/doctor` FAILS rather than warns (chat.sdd)."""
        from luxe.config import BackendEntry
        assert BackendEntry(base_url="u", engine="openrouter").needs_api_key()
        assert not BackendEntry(base_url="u", engine="llama-server").needs_api_key()

    def test_local_engines_are_not_billable(self):
        from luxe.config import BackendEntry
        assert not BackendEntry(base_url="u").is_billable()
        assert not BackendEntry(base_url="u", engine="llama-server").is_billable()
        assert not BackendEntry(base_url="u").is_openrouter()

    def test_budget_and_extras_default_to_absent(self):
        """Every pre-existing entry must be untouched by the new fields."""
        from luxe.config import BackendEntry
        e = BackendEntry(base_url="u")
        assert e.budget_usd is None
        assert e.body_extras == {}
        assert e.visible_models == []

    def test_body_extras_reach_backend_kwargs_only_when_set(self):
        """Omission is the byte-identity guarantee: a local Backend never gets
        the kwarg at all, so its request cannot change."""
        from luxe.config import BackendEntry
        assert "body_extras" not in BackendEntry(base_url="u").backend_kwargs()
        e = BackendEntry(base_url="u", engine="openrouter",
                         body_extras={"usage": {"include": True}})
        assert e.backend_kwargs()["body_extras"] == {"usage": {"include": True}}

    def test_backend_kwargs_hands_out_a_copy(self):
        """A caller mutating the kwargs must not rewrite the config object."""
        from luxe.config import BackendEntry
        e = BackendEntry(base_url="u", engine="openrouter",
                         body_extras={"usage": {"include": True}})
        e.backend_kwargs()["body_extras"]["usage"] = "clobbered"
        assert e.body_extras == {"usage": {"include": True}}

    def test_it_travels_through_a_loaded_yaml(self, tmp_path: Path):
        from luxe.config import load_config
        p = tmp_path / "c.yaml"
        p.write_text(
            "models: {monolith: M}\n"
            "roles: {monolith: {model_key: monolith}}\n"
            "backends:\n"
            "  openrouter:\n"
            "    base_url: 'https://openrouter.ai/api'\n"
            "    engine: openrouter\n"
            "    api_key_env: OPENROUTER_API_KEY\n"
            "    budget_usd: 5.0\n"
            "    body_extras: {usage: {include: true}}\n"
            "    visible_models: ['moonshotai/kimi-k3']\n")
        entry = load_config(str(p)).backend_entry("openrouter")
        assert entry.is_openrouter()
        assert entry.api_key_env == "OPENROUTER_API_KEY"
        assert entry.budget_usd == 5.0
        assert entry.body_extras == {"usage": {"include": True}}
        assert entry.visible_models == ["moonshotai/kimi-k3"]
        # keys are env-var NAMES only — never key material in YAML
        assert "api_key" not in type(entry).model_fields


class TestPerBackendVisibleModels:
    """`visible()` consults the ACTIVE entry's roster before the global one.

    The global `visible_models:` is a list of local MLX weight ids. Applied to
    a cloud catalog of ~300 third-party ids it matches nothing, so `/model`
    would offer an empty picker on the one backend where discovery matters.
    """

    def _cfg(self):
        return PipelineConfig(
            models={"monolith": "Champ"},
            roles={"monolith": RoleConfig(model_key="monolith")},
            visible_models=["Champ", "Other"],
        )

    def test_the_entry_roster_wins_when_it_has_one(self):
        from luxe.config import BackendEntry
        cfg = self._cfg()
        entry = BackendEntry(base_url="u", engine="openrouter",
                             visible_models=["org/cloud-a"])
        served = ["org/cloud-a", "org/cloud-b", "Champ"]
        assert cfg.visible(served, entry=entry) == ["org/cloud-a"]

    def test_an_entry_without_a_roster_falls_back_to_the_global_one(self):
        from luxe.config import BackendEntry
        cfg = self._cfg()
        entry = BackendEntry(base_url="u")
        assert cfg.visible(["Champ", "Stale"], entry=entry) == ["Champ"]

    def test_omitting_the_entry_is_the_pre_existing_behaviour(self):
        cfg = self._cfg()
        assert cfg.visible(["Champ", "Stale"]) == ["Champ"]

    def test_server_order_is_preserved_under_the_entry_roster(self):
        """`/model <slot> <n>` indexes must stay stable."""
        from luxe.config import BackendEntry
        cfg = self._cfg()
        entry = BackendEntry(base_url="u", visible_models=["b", "a"])
        assert cfg.visible(["a", "z", "b"], entry=entry) == ["a", "b"]


def test_chat_yaml_ships_the_openrouter_entry():
    """The shipped entry is the contract: cloud engine, env-named key, a HARD
    cap, the usage-include body extra (without which no cost comes back), and
    a per-backend shortlist. Not default — it must stay opt-in."""
    chat_cfg = Path(__file__).parent.parent / "configs" / "chat.yaml"
    cfg = load_config(chat_cfg)
    e = cfg.backend_entry("openrouter")
    assert e.is_openrouter() and e.is_billable()
    assert e.base_url == "https://openrouter.ai/api"
    assert e.api_key_env == "OPENROUTER_API_KEY"
    assert e.budget_usd == 5.00
    assert e.body_extras == {"usage": {"include": True}}
    assert e.visible_models          # a shortlist, whatever it holds today
    assert e.default is False
    assert cfg.default_backend_name() == "local"


class TestAmbientKeyFallbackIsWithheldFromCloudEndpoints:
    """A missing `OPENROUTER_API_KEY` must not silently promote the fleet's
    oMLX key into an Authorization header pointed at openrouter.ai.

    `Backend`'s empty-key fallback (env → secrets.env → Keychain, all under
    OMLX_API_KEY) exists because shells that source secrets.env without
    exporting produced permanent 401s locally. Applied to a third-party host
    it is a credential disclosure, so the entry withholds it.
    """

    def test_a_billable_entry_switches_the_fallback_off(self):
        from luxe.config import BackendEntry
        e = BackendEntry(base_url="https://openrouter.ai/api",
                         engine="openrouter")
        assert e.backend_kwargs()["key_fallback"] is False

    def test_local_entries_never_mention_it(self):
        from luxe.config import BackendEntry
        assert "key_fallback" not in BackendEntry(base_url="u").backend_kwargs()
        assert "key_fallback" not in BackendEntry(
            base_url="u", engine="llama-server").backend_kwargs()

    def test_the_backend_honours_it(self, monkeypatch):
        from luxe.backend import Backend
        import luxe.secrets as secrets

        monkeypatch.setattr(secrets, "resolve_api_key",
                            lambda *a, **k: "local-omlx-secret")
        assert Backend(base_url="http://x").api_key == "local-omlx-secret"
        assert Backend(base_url="https://openrouter.ai/api",
                       key_fallback=False).api_key == ""

    def test_an_explicit_key_still_wins_with_the_fallback_off(self):
        from luxe.backend import Backend
        b = Backend(base_url="https://openrouter.ai/api", api_key="or-key",
                    key_fallback=False)
        assert b.api_key == "or-key"


class TestBackendDefaultModel:
    """`default_model:` — which model an endpoint's unpinned slots resolve to.

    Slot defaults come from the HOST manifest (local weight ids). An endpoint
    serving somebody else's catalog has none of them, so a session opening
    there pointed at a model the server has never heard of. The ENTRY names
    the model; the engine field is never consulted, which is why this does not
    weaken chat.sdd's "engine must never change model selection".
    """

    def test_it_defaults_to_empty_so_every_entry_is_unchanged(self):
        from luxe.config import BackendEntry
        assert BackendEntry(base_url="u").default_model == ""

    def test_it_parses_from_yaml(self, tmp_path: Path):
        from luxe.config import load_config
        p = tmp_path / "c.yaml"
        p.write_text(
            "models: {monolith: M}\n"
            "roles: {monolith: {model_key: monolith}}\n"
            "backends:\n"
            "  cloud: {base_url: 'https://x/api', engine: openrouter, "
            "default_model: 'org/pick-me', default: true}\n"
            "  local: {base_url: 'http://127.0.0.1:8000'}\n")
        cfg = load_config(str(p))
        assert cfg.backend_entry("cloud").default_model == "org/pick-me"
        assert cfg.backend_entry("local").default_model == ""

    def test_it_is_independent_of_the_engine(self):
        """Config-driven, not engine-driven: an oMLX entry may declare one and
        a cloud entry may omit it."""
        from luxe.config import BackendEntry
        assert BackendEntry(base_url="u", default_model="X").default_model == "X"
        assert BackendEntry(base_url="u", engine="openrouter").default_model == ""

    def test_it_stays_out_of_the_wire_surface(self):
        """Model selection is the slot manager's business — nothing about this
        may reach `Backend(...)`."""
        from luxe.config import BackendEntry
        a = BackendEntry(base_url="u")
        b = BackendEntry(base_url="u", default_model="org/pick-me")
        assert a.backend_kwargs() == b.backend_kwargs()


def test_chat_yaml_openrouter_declares_its_default_model():
    """Without it, opening a session on this backend leaves every slot pointed
    at a local weight id OpenRouter does not serve."""
    chat_cfg = Path(__file__).parent.parent / "configs" / "chat.yaml"
    cfg = load_config(chat_cfg)
    e = cfg.backend_entry("openrouter")
    assert e.default_model
    assert e.default_model in e.visible_models
    # local entries keep resolving from the host manifest
    assert cfg.backend_entry("local").default_model == ""
    assert cfg.backend_entry("m5").default_model == ""
