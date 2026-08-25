"""Tests for multi-backend switching in `luxe chat` — SlotManager.switch_backend,
the /backend command, the unreachable hint, and the status-bar segment.

Chat-only feature (luxe.sdd carve-out): FakeBackend stands in for oMLX; no
network. Follows the test_chat_commands conventions (monkeypatched Backend on
the slots module, isolated HOME, StringIO Console).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from luxe.backend import BackendError
from luxe.chat import commands as cmd
from luxe.chat import slots as slots_mod
from luxe.chat.session import ChatSession
from luxe.config import BackendEntry, PipelineConfig, RoleConfig


class FakeBackend:
    """Configurable stand-in: per-base_url health + served models."""

    healthy: dict[str, bool] = {}
    served: dict[str, list[str]] = {}

    def __init__(self, base_url="", model="", timeout_s=600.0, api_key="",
                 stall_timeout_s=1800.0, decode_stall_timeout_s=120.0,
                 body_extras=None, key_fallback=True, send_num_ctx=True):
        self.base_url = base_url
        self.model = model
        self.timeout_s = timeout_s
        # Declared per-endpoint body fields + the ambient-key-fallback switch
        # (2026-08-17), forwarded from BackendEntry.backend_kwargs() exactly
        # like the deadlines above.
        self.body_extras = dict(body_extras or {})
        self.key_fallback = key_fallback
        self.send_num_ctx = send_num_ctx
        # Mirrors the real Backend signature — the progress deadlines (B6) are
        # forwarded from BackendEntry, so the double has to accept them or the
        # wiring test can't observe what reached the constructor.
        self.stall_timeout_s = stall_timeout_s
        self.decode_stall_timeout_s = decode_stall_timeout_s
        self.api_key = api_key
        self.unload_calls: list = []
        self.unloaded_models: list = []
        self.thermal_calls: list = []

    # `timeout_s` mirrors the real Backend (2026-08-24): `unreachable_hint`
    # asks with a few-second bound so a hung endpoint cannot hold a hint on a
    # turn that has already failed.
    def health(self, timeout_s=None):
        type(self).health_bounds.append(timeout_s)
        return type(self).healthy.get(self.base_url, True)

    health_bounds: list = []

    def list_models(self):
        return list(type(self).served.get(self.base_url, []))

    # Full `/v1/models` records + account credits — the two cloud-only reads
    # (`/model find`, `/usage`). Per-base_url like `served`.
    catalog_records: dict[str, list[dict]] = {}
    credit_data: dict[str, dict | None] = {}
    catalog_calls: dict[str, int] = {}

    def list_models_full(self):
        type(self).catalog_calls[self.base_url] = (
            type(self).catalog_calls.get(self.base_url, 0) + 1)
        return list(type(self).catalog_records.get(self.base_url, []))

    def credits(self):
        return type(self).credit_data.get(self.base_url)

    def unload_all_loaded(self, *, except_for=None):
        self.unload_calls.append(except_for)
        return {}

    # B5: per-model unload + a resident view, so tests can tell "evicted
    # everything" apart from "freed only what this session loaded".
    loaded: dict[str, list[str]] = {}

    def loaded_models(self):
        return list(type(self).loaded.get(self.base_url, []))

    def unload_model(self, model_id):
        self.unloaded_models.append(model_id)
        return True

    def thermal_guard(self, target_model, **kw):
        self.thermal_calls.append(target_model)
        return True


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    FakeBackend.healthy = {}
    FakeBackend.served = {}
    FakeBackend.catalog_records = {}
    FakeBackend.credit_data = {}
    FakeBackend.catalog_calls = {}
    FakeBackend.health_bounds = []
    monkeypatch.setattr(slots_mod, "Backend", FakeBackend)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


LOCAL = "http://127.0.0.1:8000"
M5 = "http://m5.example.ts.net:8000"


def _multi_cfg() -> PipelineConfig:
    return PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        backends={
            "local": BackendEntry(base_url=LOCAL, default=True),
            "m5": BackendEntry(base_url=M5, api_key_env="OMLX_API_KEY_M5",
                               timeout_s=2400.0),
        },
    )


def _single_cfg() -> PipelineConfig:
    return PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
    )


def _ctx(cfg) -> cmd.CommandContext:
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=120)
    c = cmd.CommandContext(console=console, session=ChatSession(),
                           slots=slots_mod.SlotManager(cfg))
    c._out = out  # type: ignore[attr-defined]
    return c


# --- SlotManager construction / switch_backend ------------------------------


def test_manager_builds_from_default_entry(monkeypatch):
    monkeypatch.setenv("OMLX_API_KEY_M5", "m5-secret")
    sm = slots_mod.SlotManager(_multi_cfg())
    assert sm.backend_name == "local"
    assert sm.backend.base_url == LOCAL
    assert sm.backend.timeout_s == 600.0


def test_manager_synthesized_local_matches_legacy(monkeypatch):
    sm = slots_mod.SlotManager(_single_cfg())
    assert sm.backend_name == "local"
    assert sm.backend.base_url == "http://127.0.0.1:8000"
    assert sm.backend.timeout_s == 600.0


def test_switch_builds_backend_from_entry_env_key_and_timeout(monkeypatch):
    monkeypatch.setenv("OMLX_API_KEY_M5", "m5-secret")
    FakeBackend.served = {M5: ["Champ"]}
    sm = slots_mod.SlotManager(_multi_cfg())
    dropped = sm.switch_backend("m5")
    assert dropped == []
    assert sm.backend_name == "m5"
    assert sm.backend.base_url == M5
    assert sm.backend.timeout_s == 2400.0
    assert sm.backend.api_key == "m5-secret"


def test_switch_missing_key_everywhere_resolves_empty(monkeypatch, tmp_path):
    """No env, no secrets.env entry, no keychain → empty key (Backend's own
    OMLX_API_KEY chain is the last fallback). Isolated from the real
    ~/.luxe/secrets.env — resolution goes through luxe.secrets now."""
    import luxe.secrets as secrets

    monkeypatch.delenv("OMLX_API_KEY_M5", raising=False)
    monkeypatch.setattr(secrets, "SECRETS_PATH", tmp_path / "absent.env")
    monkeypatch.setattr(secrets, "_from_keychain", lambda name: "")
    sm = slots_mod.SlotManager(_multi_cfg())
    sm.switch_backend("m5")
    assert sm.backend.api_key == ""


def test_switch_key_resolves_from_secrets_file(monkeypatch, tmp_path):
    """The m5 401 fix (2026-07-30): a key that lives only in secrets.env
    (never exported by the shell) still reaches the Backend."""
    import luxe.secrets as secrets

    monkeypatch.delenv("OMLX_API_KEY_M5", raising=False)
    f = tmp_path / "secrets.env"
    f.write_text("OMLX_API_KEY_M5=tailnet-key\n")
    monkeypatch.setattr(secrets, "SECRETS_PATH", f)
    monkeypatch.setattr(secrets, "_from_keychain", lambda name: "")
    sm = slots_mod.SlotManager(_multi_cfg())
    sm.switch_backend("m5")
    assert sm.backend.api_key == "tailnet-key"


def test_switch_unreachable_raises_and_stays(monkeypatch):
    FakeBackend.healthy = {M5: False}
    sm = slots_mod.SlotManager(_multi_cfg())
    old = sm.backend
    with pytest.raises(BackendError):
        sm.switch_backend("m5")
    assert sm.backend is old
    assert sm.backend_name == "local"


def test_switch_unknown_name_raises_keyerror():
    sm = slots_mod.SlotManager(_multi_cfg())
    with pytest.raises(KeyError):
        sm.switch_backend("cloud")


def test_switch_drops_unresolvable_overrides_and_keeps_valid():
    FakeBackend.served = {M5: ["Champ", "Coder-30B"]}
    sm = slots_mod.SlotManager(_multi_cfg())
    sm.set_override("code", "OnlyOnLocal-70B")
    sm.set_override("plan", "Coder-30B")
    dropped = sm.switch_backend("m5")
    assert dropped == ["code"]
    assert "code" not in sm.overrides
    assert sm.overrides["plan"] == "Coder-30B"  # resolves on m5 → kept


def test_switch_resets_resident_and_never_unloads_old_server():
    FakeBackend.served = {M5: ["Champ"]}
    sm = slots_mod.SlotManager(_multi_cfg())
    old = sm.backend
    sm.switch_backend("m5")
    assert old.unload_calls == []          # old server untouched (may be down)
    assert sm.resident == ""               # unknown residency on the new server
    b = sm.backend_for("chat")             # next turn re-confirms on NEW server
    assert b is sm.backend and b.base_url == M5
    assert b.thermal_calls == ["Champ"]
    assert sm.resident == "Champ"


# --- unreachable hint (only when the config offers an alternative) ----------


def test_hint_names_active_and_alternative():
    """The genuinely-unreachable case, VERBATIM. The fleet's outage path reads
    this string; it must not drift (chat.sdd, PLAN.md 1.2)."""
    FakeBackend.healthy = {LOCAL: False}
    sm = slots_mod.SlotManager(_multi_cfg())
    assert sm.unreachable_hint() == "local oMLX unreachable — try /backend m5"


def test_a_healthy_endpoint_is_not_called_unreachable():
    """2026-08-24 (session 168f1825a1fd): a turn that died on an oversized
    payload printed "unreachable — try /backend local" while the endpoint was
    demonstrably up, and the `local` it advised had already failed the same way
    at a smaller window. EVIDENCE.md finding 2."""
    FakeBackend.healthy = {LOCAL: True}
    sm = slots_mod.SlotManager(_multi_cfg())
    hint = sm.unreachable_hint()
    assert "unreachable" not in hint
    assert "/backend m5" not in hint      # no remedy is prescribed
    assert "answered a health check" in hint
    assert "local oMLX" in hint


def test_the_hint_probe_is_bounded():
    """It runs on a turn that has already failed, in front of a waiting user.
    A hung endpoint must not hold it for the client's generation timeout."""
    sm = slots_mod.SlotManager(_multi_cfg())
    FakeBackend.health_bounds = []
    sm.unreachable_hint()
    assert FakeBackend.health_bounds == [slots_mod._HINT_PROBE_TIMEOUT_S]


def test_a_probe_that_cannot_answer_keeps_todays_hint(monkeypatch):
    """"Cannot answer" ⇒ pre-2026-08-24 behaviour, so nothing the outage path
    depends on changes when the probe itself is the thing that is broken."""
    sm = slots_mod.SlotManager(_multi_cfg())
    monkeypatch.setattr(sm.backend, "health",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert sm.unreachable_hint() == "local oMLX unreachable — try /backend m5"


def test_hint_absent_with_single_backend():
    sm = slots_mod.SlotManager(_single_cfg())
    assert sm.unreachable_hint() is None


def test_repl_turn_backend_error_prints_hint_only_multi(monkeypatch):
    """A turn failing with BackendError keeps the REPL alive and prints the
    /backend escape hatch — but only when >1 backend is configured."""
    from luxe.chat import repl as repl_mod

    def _raise(*a, **k):
        raise BackendError("oMLX call failed: ConnectError")

    monkeypatch.setattr(repl_mod, "run_single", _raise)

    def _run(cfg) -> str:
        out = io.StringIO()
        console = Console(file=out, force_terminal=False, width=120)
        lines = iter(["hello there"])

        def reader():
            try:
                return next(lines)
            except StopIteration:
                raise EOFError

        repl_mod.run_chat_repl(cfg, "", frozenset(), console=console,
                               keep_loaded=True, reader=reader,
                               infer_task_type=lambda m: "review")
        return out.getvalue()

    # The endpoint has to be DOWN for the escape hatch to be offered at all
    # (2026-08-24): a healthy endpoint gets a true message instead.
    FakeBackend.healthy = {LOCAL: False}
    multi_out = _run(_multi_cfg())
    assert "oMLX call failed" in multi_out
    assert "try /backend m5" in multi_out

    single_out = _run(_single_cfg())
    assert "oMLX call failed" in single_out
    assert "/backend" not in single_out


def test_repl_turn_unexpected_exception_keeps_session_alive(monkeypatch):
    """Regression (2026-07-29): only BackendError was contained, so an
    OSError(ETIMEDOUT) raised while walking the repo tree ended the session.
    Any turn-path exception must now report and leave the REPL running."""
    from luxe.chat import repl as repl_mod

    def _raise(*a, **k):
        raise OSError(60, "Operation timed out")

    monkeypatch.setattr(repl_mod, "run_single", _raise)

    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=120)
    lines = iter(["hello there", "still here?"])

    def reader():
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    repl_mod.run_chat_repl(_single_cfg(), "", frozenset(), console=console,
                           keep_loaded=True, reader=reader,
                           infer_task_type=lambda m: "review")

    text = out.getvalue()
    assert "turn failed" in text
    assert "Operation timed out" in text
    # Both prompts were served — the loop kept going after the failure.
    assert text.count("turn failed") == 2


# --- /backend command dispatch ----------------------------------------------


def test_backend_lists_entries_health_and_active_marker():
    FakeBackend.healthy = {LOCAL: True, M5: False}
    c = _ctx(_multi_cfg())
    cmd.dispatch("/backend", c)
    out = c._out.getvalue()
    assert "local" in out and "m5" in out
    assert LOCAL in out and M5 in out
    assert "✓" in out and "✗" in out
    assert "active" in out


def test_backend_switch_by_name_and_number():
    c = _ctx(_multi_cfg())
    cmd.dispatch("/backend m5", c)
    assert c.slots.backend_name == "m5"
    cmd.dispatch("/backend 1", c)          # numeric pick → local
    assert c.slots.backend_name == "local"


def test_backend_switch_reports_dropped_overrides():
    FakeBackend.served = {M5: ["Champ"]}
    c = _ctx(_multi_cfg())
    c.slots.set_override("code", "LocalOnly")
    cmd.dispatch("/backend m5", c)
    out = c._out.getvalue()
    assert "dropped" in out and "code" in out


def test_backend_switch_unreachable_reports_and_stays():
    FakeBackend.healthy = {M5: False}
    c = _ctx(_multi_cfg())
    cmd.dispatch("/backend m5", c)
    assert c.slots.backend_name == "local"
    assert "unreachable" in c._out.getvalue()


def test_backend_unknown_name_message():
    c = _ctx(_multi_cfg())
    cmd.dispatch("/backend cloud", c)
    assert "Unknown backend" in c._out.getvalue()
    assert c.slots.backend_name == "local"


def test_backend_already_active():
    c = _ctx(_multi_cfg())
    cmd.dispatch("/backend local", c)
    assert "already" in c._out.getvalue()


def test_backend_listed_in_help():
    c = _ctx(_multi_cfg())
    cmd.dispatch("/help", c)
    assert "/backend" in c._out.getvalue()


# --- status-bar segment -------------------------------------------------------


def _flat(segs) -> str:
    return " · ".join("".join(t for t, _p, _r in seg.spans) for seg in segs)


def test_status_backend_segment_only_when_multi():
    from luxe.chat.status import StatusState, fields

    session = ChatSession()
    multi = slots_mod.SlotManager(_multi_cfg())
    text = _flat(fields(session, multi, "", StatusState()))
    assert "backend local" in text

    single = slots_mod.SlotManager(_single_cfg())
    text = _flat(fields(session, single, "", StatusState()))
    assert "backend" not in text


def test_status_backend_segment_tracks_switch():
    from luxe.chat.status import StatusState, fields

    sm = slots_mod.SlotManager(_multi_cfg())
    sm.switch_backend("m5")
    text = _flat(fields(ChatSession(), sm, "", StatusState()))
    assert "backend m5" in text


# --- progress deadlines wired from BackendEntry (B6) -----------------------


def test_backend_kwargs_omits_unset_progress_deadlines():
    """An entry that doesn't mention them must behave as before they existed.

    Passing None through would override Backend's own defaults with nothing,
    so the kwargs dict has to omit the keys entirely.
    """
    from luxe.config import BackendEntry

    entry = BackendEntry(base_url="http://x:8000")
    assert entry.backend_kwargs() == {"timeout_s": 600.0}


def test_backend_kwargs_forwards_set_progress_deadlines():
    from luxe.config import BackendEntry

    entry = BackendEntry(base_url="http://x:8000", timeout_s=2400,
                         stall_timeout_s=2400, decode_stall_timeout_s=180)
    assert entry.backend_kwargs() == {
        "timeout_s": 2400.0, "stall_timeout_s": 2400.0,
        "decode_stall_timeout_s": 180.0,
    }


def test_entry_without_deadlines_yields_backend_defaults():
    """The synthesized/local path must inherit backend.py's defaults."""
    from luxe.backend import Backend
    from luxe.config import BackendEntry

    entry = BackendEntry(base_url="http://127.0.0.1:8000")
    b = Backend(base_url=entry.base_url, model="m", api_key="k",
                **entry.backend_kwargs())
    default = Backend(base_url=entry.base_url, model="m", api_key="k")
    assert b.stall_timeout_s == default.stall_timeout_s
    assert b.decode_stall_timeout_s == default.decode_stall_timeout_s


def test_slot_manager_forwards_progress_deadlines(monkeypatch):
    """End-to-end: chat.yaml value -> BackendEntry -> live Backend attribute."""
    monkeypatch.setenv("OMLX_API_KEY_M5", "m5-secret")
    FakeBackend.served = {M5: ["Champ"]}
    cfg = _multi_cfg()
    cfg.backends["m5"].stall_timeout_s = 2400.0
    cfg.backends["m5"].decode_stall_timeout_s = 180.0
    sm = slots_mod.SlotManager(cfg)
    sm.switch_backend("m5")
    assert sm.backend.stall_timeout_s == 2400.0
    assert sm.backend.decode_stall_timeout_s == 180.0


def test_shipped_chat_yaml_sets_m5_progress_deadline():
    """The m5 entry needs headroom over its ~25min dense prefill.

    Backend's 1800s default leaves only ~300s above the documented 1500s
    prefill; this pins the deliberate override so a future edit that drops it
    is a test failure rather than a silent tightening.
    """
    from pathlib import Path

    from luxe.config import load_config

    cfg = load_config(Path(__file__).parents[1] / "configs" / "chat.yaml")
    m5 = cfg.backends["m5"]
    assert m5.stall_timeout_s == 2400.0
    assert m5.stall_timeout_s >= m5.timeout_s
    # local stays on the defaults — nothing to tune on a loopback endpoint.
    assert cfg.backends["local"].stall_timeout_s is None


# --- B5: single-residency must not evict other clients' models -------------


def test_local_endpoint_still_evicts_everything_on_exit():
    """Unchanged behaviour where luxe owns the box: the RAM should come back."""
    sm = slots_mod.SlotManager(_single_cfg())
    sm.unload_all()
    assert sm.backend.unload_calls == [None]      # the mass unload
    assert sm.backend.unloaded_models == []


def test_shared_endpoint_never_mass_unloads_on_exit(monkeypatch):
    """A remote endpoint may be serving another host — evict only our own."""
    monkeypatch.setenv("OMLX_API_KEY_M5", "k")
    FakeBackend.served = {M5: ["Champ"]}
    sm = slots_mod.SlotManager(_multi_cfg())
    sm.switch_backend("m5")
    sm._loaded_by_us = {"Champ"}
    sm.unload_all()
    assert sm.backend.unload_calls == [], "mass unload reached a shared endpoint"
    assert sm.backend.unloaded_models == ["Champ"]


def test_shared_endpoint_leaves_models_it_did_not_load(monkeypatch):
    """The failure that motivated B5: someone else's weights are not ours."""
    monkeypatch.setenv("OMLX_API_KEY_M5", "k")
    FakeBackend.served = {M5: ["Champ"]}
    sm = slots_mod.SlotManager(_multi_cfg())
    sm.switch_backend("m5")
    sm._loaded_by_us = set()          # we loaded nothing this session
    sm.unload_all()
    assert sm.backend.unload_calls == []
    assert sm.backend.unloaded_models == [], "evicted a model this session never loaded"


def test_shared_endpoint_skips_first_use_residency_eviction(monkeypatch):
    """Residency enforcement fires on the first turn — and used to mass-evict."""
    monkeypatch.setenv("OMLX_API_KEY_M5", "k")
    FakeBackend.served = {M5: ["Champ"]}
    FakeBackend.loaded = {M5: ["SomeoneElsesModel"]}
    try:
        sm = slots_mod.SlotManager(_multi_cfg())
        sm.switch_backend("m5")
        sm.backend_for("chat")
        assert sm.backend.unload_calls == [], "first-use eviction hit a shared endpoint"
    finally:
        FakeBackend.loaded = {}


def test_shared_endpoint_claims_a_model_it_caused_to_load(monkeypatch):
    """If the target wasn't resident, our request loads it — so it IS ours."""
    monkeypatch.setenv("OMLX_API_KEY_M5", "k")
    FakeBackend.served = {M5: ["Champ"]}
    FakeBackend.loaded = {M5: []}                 # nothing resident yet
    try:
        sm = slots_mod.SlotManager(_multi_cfg())
        sm.switch_backend("m5")
        target = sm.backend_for("chat").model
        assert target in sm._loaded_by_us
    finally:
        FakeBackend.loaded = {}


def test_shipped_chat_yaml_marks_m5_shared_and_local_owned():
    from pathlib import Path

    from luxe.config import load_config

    cfg = load_config(Path(__file__).parents[1] / "configs" / "chat.yaml")
    assert cfg.backends["m5"].is_shared() is True
    assert cfg.backends["local"].is_shared() is False


# --- the openrouter carve-out: catalog search, spend, and the hard cap ------
#
# Chat-only (luxe.sdd + chat.sdd). Everything here is billable, so the surfaces
# these pin are not cosmetics: what the catalog costs, what the session has
# spent, and the refusal that stops it spending more.

OR = "https://openrouter.ai/api"


def _or_cfg(**entry_kw) -> PipelineConfig:
    kw = dict(base_url=OR, engine="openrouter",
              api_key_env="OPENROUTER_API_KEY", budget_usd=5.0,
              body_extras={"usage": {"include": True}},
              visible_models=["org/short-listed"], default=True)
    kw.update(entry_kw)
    return PipelineConfig(
        models={"monolith": "org/short-listed"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        backends={"openrouter": BackendEntry(**kw),
                  "local": BackendEntry(base_url=LOCAL)},
    )


def _catalog() -> list[dict]:
    return [
        {"id": "moonshotai/kimi-k3",
         "pricing": {"prompt": "0.0000006", "completion": "0.0000025"},
         "supported_parameters": ["tools", "temperature"]},
        {"id": "moonshotai/kimi-k2",
         "pricing": {"prompt": "0.0000003", "completion": "0.0000012"},
         "supported_parameters": ["temperature"]},
        {"id": "org/short-listed", "pricing": {"prompt": "0", "completion": "0"},
         "supported_parameters": ["tools"]},
        {"id": "other/unrelated", "pricing": {"prompt": "0.00002",
                                              "completion": "0.00006"}},
    ]


def test_body_extras_reach_the_constructed_backend():
    """The declared merge is what makes per-request cost come back at all."""
    sm = slots_mod.SlotManager(_or_cfg())
    assert sm.backend.body_extras == {"usage": {"include": True}}


def test_local_entries_get_no_body_extras():
    """Byte-identity for every endpoint that didn't ask for anything."""
    sm = slots_mod.SlotManager(_multi_cfg())
    assert sm.backend.body_extras == {}


def test_the_per_backend_roster_governs_the_picker():
    """The global `visible_models:` names local weights; on a cloud catalog it
    would match nothing and leave `/model` empty."""
    FakeBackend.served = {OR: ["org/short-listed", "moonshotai/kimi-k3"]}
    cfg = _or_cfg()
    cfg.visible_models = ["Qwen3.6-35B-A3B-6bit"]     # the fleet-wide list
    sm = slots_mod.SlotManager(cfg)
    assert sm.available_models() == ["org/short-listed"]


def test_the_constructed_backend_is_labelled_with_its_engine():
    """The single chat construction site (`SlotManager._build_backend`) is
    what stops a failure message from asserting the wrong serving stack —
    "oMLX stream failed" on an OpenRouter turn, 2026-08-24 session
    168f1825a1fd. Set as an instance attribute like `on_reasoning`, NOT via
    `backend_kwargs()`, whose pinned contract is that `engine:` never touches
    the wire/timeout surface (tests/test_config.py)."""
    assert slots_mod.SlotManager(_or_cfg()).backend.engine_label == "OpenRouter"
    assert slots_mod.SlotManager(_multi_cfg()).backend.engine_label == "oMLX"


def test_the_label_follows_a_backend_switch():
    FakeBackend.served = {LOCAL: ["org/short-listed"]}
    sm = slots_mod.SlotManager(_or_cfg())
    sm.switch_backend("local")
    assert sm.backend.engine_label == "oMLX"


def test_engine_label_names_the_provider():
    sm = slots_mod.SlotManager(_or_cfg())
    assert sm.engine_label() == "OpenRouter"
    assert slots_mod.SlotManager(_multi_cfg()).engine_label() == "oMLX"


def test_the_unreachable_hint_names_the_active_engine():
    FakeBackend.healthy = {OR: False}
    sm = slots_mod.SlotManager(_or_cfg())
    hint = sm.unreachable_hint()
    assert "OpenRouter unreachable" in hint
    assert "/backend local" in hint


def test_the_catalog_is_fetched_once_per_endpoint():
    """~300 records over the wire; a `/model find` per keystroke would be a
    new failure mode, not a feature."""
    FakeBackend.catalog_records = {OR: _catalog()}
    sm = slots_mod.SlotManager(_or_cfg())
    assert len(sm.catalog()) == 4
    assert len(sm.catalog()) == 4
    assert FakeBackend.catalog_calls[OR] == 1


def test_the_catalog_degrades_to_empty_when_unreachable(monkeypatch):
    sm = slots_mod.SlotManager(_or_cfg())
    monkeypatch.setattr(sm.backend, "list_models_full",
                        lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert sm.catalog() == []


def test_the_catalog_teaches_modelcaps_about_tool_support():
    """A provider catalog states `supported_parameters` — a first-party answer
    where `from_template` can only fail open (the weights aren't on this disk)."""
    from luxe.chat import modelcaps

    modelcaps.reset_cache()
    FakeBackend.catalog_records = {OR: _catalog()}
    sm = slots_mod.SlotManager(_or_cfg())
    sm.catalog()
    assert modelcaps.for_model(sm.backend, "moonshotai/kimi-k3").usable
    assert not modelcaps.for_model(sm.backend, "moonshotai/kimi-k2").usable
    modelcaps.reset_cache()


class TestModelFind:
    def _ctx_with_catalog(self):
        FakeBackend.served = {OR: ["org/short-listed"]}
        FakeBackend.catalog_records = {OR: _catalog()}
        return _ctx(_or_cfg())

    def test_it_matches_case_insensitively_on_a_substring(self):
        c = self._ctx_with_catalog()
        cmd.dispatch("/model find KIMI", c)
        out = c._out.getvalue()
        assert "moonshotai/kimi-k3" in out
        assert "moonshotai/kimi-k2" in out
        assert "other/unrelated" not in out

    def test_it_shows_prices_per_million_tokens(self):
        """Catalogs quote per-TOKEN figures; nobody budgets in those."""
        c = self._ctx_with_catalog()
        cmd.dispatch("/model find kimi-k3", c)
        out = c._out.getvalue()
        assert "$0.60" in out and "$2.50" in out

    def test_it_flags_a_model_that_cannot_call_tools(self):
        c = self._ctx_with_catalog()
        cmd.dispatch("/model find kimi-k2", c)
        assert "no tool support" in c._out.getvalue()

    def test_it_names_the_next_command(self):
        c = self._ctx_with_catalog()
        cmd.dispatch("/model find kimi", c)
        assert "/model all" in c._out.getvalue()

    def test_no_match_says_so_with_the_catalog_size(self):
        c = self._ctx_with_catalog()
        cmd.dispatch("/model find zzz", c)
        out = c._out.getvalue()
        assert "No model id contains" in out and "4" in out

    def test_it_requires_a_query(self):
        c = self._ctx_with_catalog()
        cmd.dispatch("/model find", c)
        assert "Usage: /model find" in c._out.getvalue()

    def test_find_is_not_mistaken_for_a_slot(self):
        c = self._ctx_with_catalog()
        cmd.dispatch("/model find kimi", c)
        assert "Unknown slot" not in c._out.getvalue()

    def test_an_unreachable_catalog_names_the_engine(self):
        FakeBackend.served = {}
        FakeBackend.catalog_records = {}
        c = _ctx(_or_cfg())
        cmd.dispatch("/model find kimi", c)
        assert "OpenRouter" in c._out.getvalue()

    def test_the_plain_listing_points_at_find_when_the_roster_hides_models(self):
        FakeBackend.served = {OR: ["org/short-listed", "a", "b"]}
        c = _ctx(_or_cfg())
        cmd.dispatch("/model", c)
        assert "/model find" in c._out.getvalue()

    def test_a_local_listing_does_not_mention_find(self):
        """No per-backend roster ⇒ nothing is hidden ⇒ no extra line."""
        FakeBackend.served = {LOCAL: ["Champ"]}
        c = _ctx(_multi_cfg())
        cmd.dispatch("/model", c)
        assert "/model find" not in c._out.getvalue()


class TestSpendAndTheHardCap:
    def test_a_fresh_session_has_spent_nothing(self):
        from luxe.chat import cost as cost_mod
        c = _ctx(_or_cfg())
        assert cost_mod.spent(c.session) == 0.0
        assert cost_mod.is_billable(c.slots)
        assert cost_mod.cap_usd(c.session, c.slots) == 5.0
        assert cost_mod.refusal(c.session, c.slots) is None

    def test_a_local_backend_is_never_billable_and_never_capped(self):
        from luxe.chat import cost as cost_mod
        c = _ctx(_multi_cfg())
        assert not cost_mod.is_billable(c.slots)
        assert cost_mod.cap_usd(c.session, c.slots) is None
        c.session.session_cost_usd = 99.0     # even then: no cap, no refusal
        assert cost_mod.refusal(c.session, c.slots) is None

    def test_turn_costs_accumulate_and_reach_the_status_bar(self):
        from dataclasses import dataclass

        from luxe.chat import cost as cost_mod
        from luxe.chat.status import StatusState

        @dataclass
        class _Result:
            cost_usd: float = 0.0

        c = _ctx(_or_cfg())
        state = StatusState()
        cost_mod.record_turn(c.session, _Result(0.01), state)
        cost_mod.record_turn(c.session, _Result(0.02), state)
        assert c.session.session_cost_usd == pytest.approx(0.03)
        assert c.session.turn_costs == [0.01, 0.02]
        assert state.session_cost_usd == pytest.approx(0.03)

    def test_a_zero_cost_turn_is_not_recorded_as_a_turn(self):
        """A local endpoint reports no cost at all; a list of 0.00 entries
        would make `/usage` claim a history it does not have."""
        from luxe.chat import cost as cost_mod

        class _R:
            cost_usd = 0.0

        c = _ctx(_or_cfg())
        assert cost_mod.record_turn(c.session, _R()) == 0.0
        assert c.session.turn_costs == []

    def test_reaching_the_cap_refuses_the_next_turn(self):
        from luxe.chat import cost as cost_mod
        c = _ctx(_or_cfg(budget_usd=0.05))
        c.session.session_cost_usd = 0.05
        msg = cost_mod.refusal(c.session, c.slots)
        assert msg is not None
        # Every refusal names its unlock (chat.sdd): spend, cap, and the raise.
        assert "0.050" in msg and "/usage budget" in msg

    def test_an_uncapped_billable_entry_never_refuses(self):
        from luxe.chat import cost as cost_mod
        c = _ctx(_or_cfg(budget_usd=None))
        c.session.session_cost_usd = 1000.0
        assert cost_mod.cap_usd(c.session, c.slots) is None
        assert cost_mod.refusal(c.session, c.slots) is None

    def test_the_status_bar_shows_spend_only_on_a_billable_backend(self):
        from luxe.chat import status as status_mod
        from luxe.chat.status import StatusState

        c = _ctx(_or_cfg())
        c.session.session_cost_usd = 0.042
        segs = status_mod.fields(c.session, c.slots, "", StatusState())
        text = " · ".join("".join(t for t, _p, _r in s.spans) for s in segs)
        assert "$0.042" in text and "$5.00" in text

        local = _ctx(_multi_cfg())
        local_segs = status_mod.fields(local.session, local.slots, "",
                                       StatusState())
        local_text = " · ".join(
            "".join(t for t, _p, _r in s.spans) for s in local_segs)
        assert "$" not in local_text

    def test_the_footer_reports_a_billed_turn_and_omits_an_unbilled_one(self):
        from luxe.chat.render import render_footer_text

        class _R:
            steps = 1
            tool_calls_total = 0
            wall_s = 1.0
            prompt_tokens = 10
            completion_tokens = 5
            last_prompt_tokens = 10
            peak_context_pressure = 0.1
            cost_usd = 0.0

        assert "cost:" not in render_footer_text("chat", "m", _R())
        _R.cost_usd = 0.0134
        assert "cost: $0.013" in render_footer_text("chat", "m", _R())


class TestUsageCommand:
    def _ctx_billable(self, **kw):
        FakeBackend.served = {OR: ["org/short-listed"]}
        return _ctx(_or_cfg(**kw))

    def test_it_reports_per_turn_costs_and_the_session_total(self):
        c = self._ctx_billable()
        c.session.turn_costs = [0.01, 0.025]
        c.session.session_cost_usd = 0.035
        cmd.dispatch("/usage", c)
        out = c._out.getvalue()
        assert "per turn" in out and "$0.010" in out and "$0.025" in out
        assert "session total" in out and "$0.035" in out

    def test_it_shows_the_cap_and_the_headroom(self):
        c = self._ctx_billable()
        c.session.session_cost_usd = 1.0
        cmd.dispatch("/usage", c)
        out = c._out.getvalue()
        assert "$5.00" in out and "$4.00" in out

    def test_it_works_before_any_turn_has_been_billed(self):
        c = self._ctx_billable()
        cmd.dispatch("/usage", c)
        assert "no billed turns yet" in c._out.getvalue()

    def test_key_limit_and_usage_are_fetched_and_rendered(self):
        # The /v1/key shape: the key's own limit/usage (account-wide
        # /v1/credits is management-key-only and 403s for inference keys).
        FakeBackend.credit_data = {OR: {"limit": 20.0, "usage": 4.5}}
        c = self._ctx_billable()
        cmd.dispatch("/usage", c)
        out = c._out.getvalue()
        assert "key" in out and "$15.50" in out

    def test_an_uncapped_key_renders_usage_without_a_limit(self):
        FakeBackend.credit_data = {OR: {"limit": None, "usage": 4.5}}
        c = self._ctx_billable()
        cmd.dispatch("/usage", c)
        out = c._out.getvalue()
        assert "used $4.50" in out and "limit" not in out

    def test_offline_key_lookup_degrades_to_unreachable(self):
        FakeBackend.credit_data = {OR: None}
        c = self._ctx_billable()
        cmd.dispatch("/usage", c)
        assert "key: unreachable" in c._out.getvalue()

    def test_a_local_backend_says_it_is_not_billed(self):
        c = _ctx(_multi_cfg())
        cmd.dispatch("/usage", c)
        out = c._out.getvalue()
        assert "not billed" in out
        assert "session total" not in out

    def test_budget_raises_the_cap_for_this_session_only(self):
        from luxe.chat import cost as cost_mod
        c = self._ctx_billable()
        c.session.session_cost_usd = 5.0
        assert cost_mod.refusal(c.session, c.slots) is not None
        cmd.dispatch("/usage budget 20", c)
        assert c.session.budget_override_usd == 20.0
        assert cost_mod.cap_usd(c.session, c.slots) == 20.0
        assert cost_mod.refusal(c.session, c.slots) is None
        # config untouched — a raise is a decision about THIS session
        assert c.slots.cfg.backend_entry("openrouter").budget_usd == 5.0

    def test_budget_rejects_junk_without_changing_anything(self):
        c = self._ctx_billable()
        cmd.dispatch("/usage budget lots", c)
        assert c.session.budget_override_usd is None
        assert "Not a dollar amount" in c._out.getvalue()

    def test_budget_needs_an_amount(self):
        c = self._ctx_billable()
        cmd.dispatch("/usage budget", c)
        assert "Usage: /usage budget" in c._out.getvalue()


def test_status_carries_a_cost_row_only_where_it_can_move():
    FakeBackend.served = {OR: ["org/short-listed"]}
    c = _ctx(_or_cfg())
    c.session.session_cost_usd = 0.25
    cmd.dispatch("/status", c)
    out = c._out.getvalue()
    assert "cost" in out and "$0.25" in out and "$5.00" in out

    local = _ctx(_multi_cfg())
    cmd.dispatch("/status", local)
    assert "$" not in local._out.getvalue()


def test_the_backend_failure_hint_drops_tunnel_advice_on_a_cloud_entry():
    """`/planeproxy` cannot help an endpoint that rides no tunnel — the two
    real causes there are the key and the account balance."""
    FakeBackend.healthy = {OR: False}
    cfg = _or_cfg()
    cfg.backends["local"].default = True
    cfg.backends["openrouter"].default = False
    c = _ctx(cfg)
    cmd.dispatch("/backend openrouter", c)
    out = c._out.getvalue()
    assert "OPENROUTER_API_KEY" in out and "/usage" in out
    assert "planeproxy" not in out


# --- per-endpoint `default_model:` -----------------------------------------
#
# The gap this closes: slot defaults come from the host manifest (local weight
# ids), so a session that opened on the cloud backend sat pointed at a model
# OpenRouter has never heard of until `/model all <id>` was typed. The entry
# names the model instead. It is a DEFAULT and yields to anything the user
# actually chose.

DM = "org/entry-default"


def _dm_cfg(**entry_kw) -> PipelineConfig:
    """local (no default_model) + cloud (declares one). local is the default
    entry so a switch can be exercised in both directions."""
    kw = dict(base_url=OR, engine="openrouter", default_model=DM,
              api_key_env="OPENROUTER_API_KEY")
    kw.update(entry_kw)
    return PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        backends={"local": BackendEntry(base_url=LOCAL, default=True),
                  "cloud": BackendEntry(**kw)},
    )


class TestEntryDefaultModel:
    def test_starting_on_such_a_backend_selects_it(self):
        cfg = _dm_cfg()
        cfg.backends["local"].default = False
        cfg.backends["cloud"].default = True
        sm = slots_mod.SlotManager(cfg)
        assert sm.slot_models() == {"chat": DM, "plan": DM, "code": DM}
        # …and the manager never believed the manifest model was warm there
        assert sm.resident == DM
        assert sm.backend.model == DM

    def test_a_backend_without_the_field_is_byte_identical(self):
        """The control: local resolution must be untouched."""
        sm = slots_mod.SlotManager(_dm_cfg())
        assert sm.slot_models() == {"chat": "Champ", "plan": "Champ",
                                    "code": "Champ"}
        assert sm.overrides == {}
        assert sm.resident == "Champ"
        assert sm.default_model_applied == ""

    def test_a_config_with_no_backends_block_is_untouched(self):
        sm = slots_mod.SlotManager(_single_cfg())
        assert sm.overrides == {}
        assert sm.slot_models()["chat"] == "Champ"

    def test_switching_to_it_applies_it_to_every_slot(self):
        FakeBackend.served = {OR: [DM]}
        sm = slots_mod.SlotManager(_dm_cfg())
        assert sm.slot_models()["chat"] == "Champ"
        sm.switch_backend("cloud")
        assert sm.slot_models() == {"chat": DM, "plan": DM, "code": DM}
        assert sm.default_model_applied == DM

    def test_an_explicit_model_choice_after_the_switch_wins(self):
        FakeBackend.served = {OR: [DM, "org/i-picked-this"]}
        sm = slots_mod.SlotManager(_dm_cfg())
        sm.switch_backend("cloud")
        sm.set_override("chat", "org/i-picked-this")
        assert sm.model_for("chat") == "org/i-picked-this"
        assert sm.model_for("plan") == DM          # untouched slots keep it

    def test_a_surviving_pre_switch_override_is_not_clobbered(self):
        """An override the new server DOES serve is a typed instruction that
        outlived the switch — a default must not overwrite it."""
        FakeBackend.served = {OR: [DM, "org/pinned"]}
        sm = slots_mod.SlotManager(_dm_cfg())
        sm.set_override("chat", "org/pinned")
        sm.switch_backend("cloud")
        assert sm.model_for("chat") == "org/pinned"
        assert sm.model_for("code") == DM

    def test_a_startup_slot_model_flag_outranks_it(self):
        """`--chat-model` arrives as a non-empty SlotConfig.model_key
        (chat/launch._apply_slot_overrides). It is a user choice too."""
        from luxe.config import ChatSlots, SlotConfig

        cfg = _dm_cfg()
        cfg.backends["local"].default = False
        cfg.backends["cloud"].default = True
        cfg.models["_slot_chat"] = "org/cli-flag"
        cfg.slots = ChatSlots(chat=SlotConfig(model_key="_slot_chat"))
        sm = slots_mod.SlotManager(cfg)
        assert sm.model_for("chat") == "org/cli-flag"
        assert sm.model_for("plan") == DM

    def test_switching_away_returns_the_slots_to_the_manifest(self):
        """No counterpart logic needed: switch_backend already drops overrides
        the new server doesn't serve."""
        FakeBackend.served = {OR: [DM], LOCAL: ["Champ"]}
        sm = slots_mod.SlotManager(_dm_cfg())
        sm.switch_backend("cloud")
        assert sm.model_for("chat") == DM
        dropped = sm.switch_backend("local")
        assert set(dropped) == {"chat", "plan", "code"}
        assert sm.slot_models()["chat"] == "Champ"
        assert sm.default_model_applied == ""

    def test_an_empty_or_whitespace_declaration_selects_nothing(self):
        sm = slots_mod.SlotManager(_dm_cfg(default_model="   "))
        assert sm.overrides == {}

    def test_the_switch_is_announced_not_silent(self):
        """A backend hop that quietly changes which model answers is the
        failure shape auto-degrade exists to prevent."""
        FakeBackend.served = {OR: [DM]}
        c = _ctx(_dm_cfg())
        cmd.dispatch("/backend cloud", c)
        out = c._out.getvalue()
        assert DM in out and "default_model" in out
        assert "/model all" in out

    def test_a_switch_to_a_plain_backend_says_nothing_extra(self):
        FakeBackend.served = {M5: ["Champ"]}
        c = _ctx(_multi_cfg())
        cmd.dispatch("/backend m5", c)
        assert "default_model" not in c._out.getvalue()


# --- cloud-aware context handling ------------------------------------------
#
# The live failure (2026-08-17): a session on a 1,048,576-token hosted model
# ran at the 32K local default, and `/ctx` reported tiers as needing RAM the
# machine doesn't have — for a window the provider was already serving. Both
# numbers were about THIS box's KV cache, which is not in the loop there.

K3 = "moonshotai/kimi-k3"
K3_CTX = 1_048_576


def _ctx_catalog() -> list[dict]:
    return [
        {"id": K3, "context_length": K3_CTX,
         "top_provider": {"context_length": 262144},
         "pricing": {"prompt": "0.0000006", "completion": "0.0000025"},
         "supported_parameters": ["tools"]},
        {"id": "org/no-window-stated",
         "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "org/provider-only",
         "top_provider": {"context_length": 200000}},
    ]


def _cloud_ctx_cfg(**entry_kw) -> PipelineConfig:
    kw = dict(base_url=OR, engine="openrouter", budget_usd=5.0,
              default_model=K3, visible_models=[K3], default=True)
    kw.update(entry_kw)
    return PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith", num_ctx=32768,
                                      num_ctx_max=262144)},
        backends={"cloud": BackendEntry(**kw),
                  "local": BackendEntry(base_url=LOCAL)},
    )


class TestCatalogContextLength:
    def test_the_catalog_window_becomes_the_ceiling(self):
        FakeBackend.catalog_records = {OR: _ctx_catalog()}
        sm = slots_mod.SlotManager(_cloud_ctx_cfg())
        assert sm.catalog_context_length(K3) == K3_CTX
        # …and it OUTRANKS the role's num_ctx_max, which is about this box.
        assert sm.ctx_ceiling("chat") == K3_CTX

    def test_top_provider_is_the_fallback_field(self):
        FakeBackend.catalog_records = {OR: _ctx_catalog()}
        sm = slots_mod.SlotManager(_cloud_ctx_cfg(default_model="org/provider-only"))
        assert sm.catalog_context_length("org/provider-only") == 200000

    def test_a_model_with_no_stated_window_falls_back_to_the_role(self):
        FakeBackend.catalog_records = {OR: _ctx_catalog()}
        sm = slots_mod.SlotManager(
            _cloud_ctx_cfg(default_model="org/no-window-stated"))
        assert sm.catalog_context_length("org/no-window-stated") == 0
        assert sm.ctx_ceiling("chat") == 262144        # role num_ctx_max

    def test_a_local_endpoint_never_warms_the_catalog_for_this(self):
        """`ctx_ceiling` runs every turn; oMLX reports ids only, so the GET
        could never answer and must not be paid."""
        FakeBackend.served = {LOCAL: ["Champ"]}
        cfg = _cloud_ctx_cfg()
        cfg.backends["cloud"].default = False
        cfg.backends["local"].default = True
        sm = slots_mod.SlotManager(cfg)
        assert sm.ctx_ceiling("chat") == 262144
        assert FakeBackend.catalog_calls.get(LOCAL) is None

    def test_a_local_endpoint_keeps_the_manifest_cap(self):
        """Regression guard for the old path: nothing about the new ceiling
        may bypass the per-model `ctx_max`."""
        from luxe.config import HostManifest
        cfg = _cloud_ctx_cfg()
        cfg.backends["cloud"].default = False
        cfg.backends["local"].default = True
        cfg.hosts = {"testhost": HostManifest(main="Champ",
                                              ctx_max={"Champ": 16384})}
        sm = slots_mod.SlotManager(cfg, manifest_host="testhost")
        assert sm.ctx_ceiling("chat") == 16384

    def test_it_degrades_to_zero_when_the_catalog_is_unreachable(self):
        FakeBackend.catalog_records = {}
        sm = slots_mod.SlotManager(_cloud_ctx_cfg())
        assert sm.catalog_context_length(K3) == 0
        assert sm.ctx_ceiling("chat") == 262144


class TestBillableDefaultWindow:
    def test_a_billable_backend_defaults_to_128k(self):
        """A cost bound, not a capability one: the window sets how much
        history each metered step re-sends."""
        from luxe.chat.session import BILLABLE_DEFAULT_NUM_CTX
        FakeBackend.catalog_records = {OR: _ctx_catalog()}
        sm = slots_mod.SlotManager(_cloud_ctx_cfg())
        assert sm.default_num_ctx("chat") == BILLABLE_DEFAULT_NUM_CTX == 131072
        assert sm.role_for("chat").num_ctx == 32768      # role untouched

    def test_a_local_backend_keeps_the_roles_window(self):
        cfg = _cloud_ctx_cfg()
        cfg.backends["cloud"].default = False
        cfg.backends["local"].default = True
        sm = slots_mod.SlotManager(cfg)
        assert sm.default_num_ctx("chat") == 32768

    def test_the_default_is_clamped_to_a_smaller_ceiling(self):
        """A billable model with a 64K window must not default above it."""
        FakeBackend.catalog_records = {OR: [{"id": K3, "context_length": 65536}]}
        sm = slots_mod.SlotManager(_cloud_ctx_cfg())
        assert sm.default_num_ctx("chat") == 65536

    def test_a_turn_uses_the_billable_default_without_any_ctx_command(self):
        """The whole point: no `/ctx` typed, and the turn still runs wide."""
        from luxe.chat import repl as repl_mod

        FakeBackend.catalog_records = {OR: _ctx_catalog()}
        FakeBackend.served = {OR: [K3]}
        cfg = _cloud_ctx_cfg()
        sm = slots_mod.SlotManager(cfg)
        session = ChatSession()
        prep = repl_mod.prepare_turn("hello", session, sm, cfg, frozenset(),
                                     lambda m: "review")
        assert prep.role_cfg.num_ctx == 131072
        assert prep.ctx_ceiling == K3_CTX

    def test_an_explicit_ctx_override_still_wins(self):
        from luxe.chat import repl as repl_mod

        FakeBackend.catalog_records = {OR: _ctx_catalog()}
        FakeBackend.served = {OR: [K3]}
        cfg = _cloud_ctx_cfg()
        sm = slots_mod.SlotManager(cfg)
        session = ChatSession(num_ctx_override=500_000)
        prep = repl_mod.prepare_turn("hello", session, sm, cfg, frozenset(),
                                     lambda m: "review")
        assert prep.role_cfg.num_ctx == 500_000

    def test_a_local_turn_is_byte_identical(self):
        """The control: no override, local backend ⇒ the role's own window,
        exactly as before this change."""
        from luxe.chat import repl as repl_mod

        cfg = _cloud_ctx_cfg()
        cfg.backends["cloud"].default = False
        cfg.backends["local"].default = True
        sm = slots_mod.SlotManager(cfg)
        prep = repl_mod.prepare_turn("hello", ChatSession(), sm, cfg,
                                     frozenset(), lambda m: "review")
        assert prep.role_cfg.num_ctx == 32768


class TestCtxCommandOnACloudBackend:
    def _ctx_cloud(self, monkeypatch):
        from luxe.chat import origin as origin_mod
        FakeBackend.catalog_records = {OR: _ctx_catalog()}
        FakeBackend.served = {OR: [K3]}
        # The model is served by another machine — its KV cache is not ours.
        monkeypatch.setattr(
            origin_mod, "cached_origin_for",
            lambda backend, model: origin_mod.ModelOrigin(kind="remote",
                                                          model_id=model))
        return _ctx(_cloud_ctx_cfg())

    def test_every_tier_is_inside_range_on_a_1m_model(self, monkeypatch):
        c = self._ctx_cloud(monkeypatch)
        cmd.dispatch("/ctx", c)
        assert "(>max)" not in c._out.getvalue()

    def test_ram_warnings_are_suppressed_for_remote_weights(self, monkeypatch):
        """They are local-KV arithmetic; here they would tell the user their
        machine is too small for a window the provider already serves."""
        c = self._ctx_cloud(monkeypatch)
        cmd.dispatch("/ctx", c)
        out = c._out.getvalue()
        assert "needs" not in out and "GB" not in out

    def test_the_help_line_talks_about_billing_not_ram(self, monkeypatch):
        c = self._ctx_cloud(monkeypatch)
        cmd.dispatch("/ctx", c)
        out = c._out.getvalue()
        assert "billable prompt tokens" in out
        assert "KV-cache" not in out

    def test_it_reports_the_real_max_and_the_billable_default(self, monkeypatch):
        c = self._ctx_cloud(monkeypatch)
        cmd.dispatch("/ctx", c)
        out = c._out.getvalue()
        assert str(K3_CTX) in out          # · max 1048576
        assert "131072" in out             # the window in force

    def test_an_absolute_size_inside_the_real_ceiling_is_not_clamped(
            self, monkeypatch):
        c = self._ctx_cloud(monkeypatch)
        cmd.dispatch("/ctx 500k", c)
        assert c.session.num_ctx_override == 500_000
        assert "clamped" not in c._out.getvalue()

    def test_past_the_real_ceiling_the_clamp_names_the_endpoint(self, monkeypatch):
        c = self._ctx_cloud(monkeypatch)
        cmd.dispatch("/ctx 2m", c)
        out = c._out.getvalue()
        assert f"clamped to {K3_CTX}" in out
        assert "as the endpoint reports it" in out
        assert "num_ctx_max" not in out    # not this box's config to raise

    def test_the_status_bar_shows_the_effective_window(self, monkeypatch):
        from luxe.chat import status as status_mod
        from luxe.chat.status import StatusState

        c = self._ctx_cloud(monkeypatch)
        c.session.num_ctx_override = 500_000
        segs = status_mod.fields(c.session, c.slots, "", StatusState())
        text = " · ".join("".join(t for t, _p, _r in s.spans) for s in segs)
        assert "488K" in text              # 500000 rendered in the K convention


def test_num_ctx_is_not_sent_on_the_openrouter_wire():
    """An Ollama/oMLX-ism a hosted provider ignores. Declared per ENDPOINT
    (`backend_kwargs`), not tested for inside the body assembly."""
    from luxe.config import BackendEntry

    cloud = BackendEntry(base_url=OR, engine="openrouter")
    assert cloud.backend_kwargs()["send_num_ctx"] is False
    # every local entry omits the kwarg, so nothing that sent it stops
    assert "send_num_ctx" not in BackendEntry(base_url=LOCAL).backend_kwargs()
    assert "send_num_ctx" not in BackendEntry(
        base_url=LOCAL, engine="llama-server").backend_kwargs()


def test_the_backend_honours_send_num_ctx():
    import json as _json

    import httpx

    from luxe.backend import Backend

    seen: dict = {}

    def _handler(request):
        seen.update(_json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    def _run(**kw):
        seen.clear()
        b = Backend(base_url="http://test", model="m", api_key="k", **kw)
        b._client = httpx.Client(base_url="http://test",
                                 transport=httpx.MockTransport(_handler))
        b.chat([{"role": "user", "content": "hi"}], num_ctx=131072)
        return dict(seen)

    assert _run()["num_ctx"] == 131072            # default: unchanged
    assert "num_ctx" not in _run(send_num_ctx=False)


# --- /reasoning: effort control on a thinking model ------------------------
#
# kimi-k3 is a REASONING model and every reasoning token is billed. A
# one-sentence question measured 255 characters of answer against 3,568 of
# reasoning and 792 billed completion tokens (2026-08-17). Effort is therefore
# a COST control first, and it rides the same declared-body mechanism as
# everything else on this endpoint.


def _reasoning_cfg(effort: str = "low") -> PipelineConfig:
    return PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        backends={"cloud": BackendEntry(base_url=OR, engine="openrouter",
                                        reasoning_effort=effort,
                                        default_model=K3, default=True),
                  "local": BackendEntry(base_url=LOCAL)},
    )


class TestReasoningEffortConfig:
    def test_the_entry_renders_it_into_the_declared_body(self):
        from luxe.config import BackendEntry
        e = BackendEntry(base_url=OR, engine="openrouter",
                         reasoning_effort="high")
        assert e.backend_kwargs()["body_extras"]["reasoning"] == {"effort": "high"}

    def test_off_asks_the_provider_not_to_return_it(self):
        from luxe.config import BackendEntry
        e = BackendEntry(base_url=OR, engine="openrouter",
                         reasoning_effort="off")
        assert e.backend_kwargs()["body_extras"]["reasoning"] == {"exclude": True}

    def test_unset_sends_nothing(self):
        from luxe.config import BackendEntry
        e = BackendEntry(base_url=OR, engine="openrouter")
        assert "reasoning" not in e.backend_kwargs().get("body_extras", {})

    def test_it_is_confined_to_the_engine_that_reads_it(self):
        """Posting an unknown vendor field at oMLX buys nothing."""
        from luxe.config import BackendEntry
        e = BackendEntry(base_url=LOCAL, reasoning_effort="high")
        assert "body_extras" not in e.backend_kwargs()

    def test_it_merges_beside_the_other_declared_extras(self):
        from luxe.config import BackendEntry
        e = BackendEntry(base_url=OR, engine="openrouter",
                         reasoning_effort="low",
                         body_extras={"usage": {"include": True}})
        extras = e.backend_kwargs()["body_extras"]
        assert extras == {"usage": {"include": True},
                          "reasoning": {"effort": "low"}}

    def test_it_reaches_the_constructed_backend(self):
        sm = slots_mod.SlotManager(_reasoning_cfg("medium"))
        assert sm.backend.body_extras["reasoning"] == {"effort": "medium"}

    def test_the_translation_table(self):
        from luxe.config import reasoning_extras
        assert reasoning_extras("low") == {"effort": "low"}
        assert reasoning_extras("HIGH") == {"effort": "high"}
        assert reasoning_extras("off") == {"exclude": True}
        assert reasoning_extras("default") is None
        assert reasoning_extras("") is None
        assert reasoning_extras("nonsense") is None

    def test_the_shipped_chat_yaml_asks_for_the_cheap_tier(self):
        from pathlib import Path

        from luxe.config import load_config
        cfg = load_config(Path(__file__).parents[1] / "configs" / "chat.yaml")
        e = cfg.backend_entry("openrouter")
        assert e.reasoning_effort == "low"
        assert e.backend_kwargs()["body_extras"]["reasoning"] == {"effort": "low"}
        # local entries are untouched
        assert cfg.backend_entry("local").reasoning_effort == ""


class TestReasoningCommand:
    def _cloud(self, effort="low"):
        FakeBackend.served = {OR: [K3]}
        return _ctx(_reasoning_cfg(effort))

    def test_bare_reasoning_reports_instead_of_toggling(self):
        """It grew from a bare display toggle into the effort control, so the
        no-arg form now says what is in force."""
        c = self._cloud()
        before = c.session.show_reasoning
        cmd.dispatch("/reasoning", c)
        out = c._out.getvalue()
        assert "effort" in out and "low" in out
        assert c.session.show_reasoning is before

    def test_setting_an_effort_rewrites_the_live_body(self):
        c = self._cloud()
        cmd.dispatch("/reasoning high", c)
        assert c.slots.backend.body_extras["reasoning"] == {"effort": "high"}
        assert "high" in c._out.getvalue()

    def test_off_sends_exclude(self):
        c = self._cloud()
        cmd.dispatch("/reasoning off", c)
        assert c.slots.backend.body_extras["reasoning"] == {"exclude": True}

    def test_default_removes_the_field_entirely(self):
        c = self._cloud()
        cmd.dispatch("/reasoning default", c)
        assert "reasoning" not in c.slots.backend.body_extras

    def test_show_and_hide_still_drive_the_live_display(self):
        c = self._cloud()
        assert c.session.show_reasoning is False
        cmd.dispatch("/reasoning show", c)
        assert c.session.show_reasoning is True
        cmd.dispatch("/reasoning hide", c)
        assert c.session.show_reasoning is False

    def test_show_when_already_shown_is_a_no_op(self):
        c = self._cloud()
        c.session.show_reasoning = True
        cmd.dispatch("/reasoning show", c)
        assert c.session.show_reasoning is True
        assert "already" in c._out.getvalue()

    def test_an_unknown_setting_changes_nothing(self):
        c = self._cloud()
        cmd.dispatch("/reasoning ludicrous", c)
        assert c.slots.backend.body_extras["reasoning"] == {"effort": "low"}
        assert "Unknown reasoning setting" in c._out.getvalue()

    def test_effort_is_refused_on_an_engine_that_ignores_it(self):
        """A setting that changes nothing is worse than a message saying so."""
        cfg = _reasoning_cfg()
        cfg.backends["cloud"].default = False
        cfg.backends["local"].default = True
        c = _ctx(cfg)
        cmd.dispatch("/reasoning high", c)
        out = c._out.getvalue()
        assert "no effect on oMLX" in out
        assert "reasoning" not in (c.slots.backend.body_extras or {})

    def test_show_hide_still_work_on_a_local_backend(self):
        """The display toggle is engine-agnostic — it is about the screen."""
        cfg = _reasoning_cfg()
        cfg.backends["cloud"].default = False
        cfg.backends["local"].default = True
        c = _ctx(cfg)
        cmd.dispatch("/reasoning show", c)
        assert c.session.show_reasoning is True

    def test_the_bare_form_says_so_on_an_unsupported_engine(self):
        cfg = _reasoning_cfg()
        cfg.backends["cloud"].default = False
        cfg.backends["local"].default = True
        c = _ctx(cfg)
        cmd.dispatch("/reasoning", c)
        assert "no effect on oMLX" in c._out.getvalue()

    def test_status_shows_the_effort_where_it_applies(self):
        c = self._cloud()
        cmd.dispatch("/reasoning high", c)
        cmd.dispatch("/status", c)
        out = c._out.getvalue()
        assert "reasoning" in out and "effort high" in out

    def test_status_omits_it_on_a_local_backend(self):
        cfg = _reasoning_cfg()
        cfg.backends["cloud"].default = False
        cfg.backends["local"].default = True
        c = _ctx(cfg)
        cmd.dispatch("/status", c)
        assert "effort" not in c._out.getvalue()


def test_the_reasoning_block_goes_top_level_on_the_wire():
    """Vendor fields are TOP-LEVEL, never `extra_body` (2026-08-11)."""
    import json as _json

    import httpx

    from luxe.backend import Backend

    seen: dict = {}

    def _handler(request):
        seen.update(_json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    b = Backend(base_url="http://test", model="m", api_key="k",
                body_extras={"reasoning": {"effort": "low"}})
    b._client = httpx.Client(base_url="http://test",
                             transport=httpx.MockTransport(_handler))
    b.chat([{"role": "user", "content": "hi"}])
    assert seen["reasoning"] == {"effort": "low"}
    assert "extra_body" not in seen
