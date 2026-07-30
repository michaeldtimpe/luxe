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
