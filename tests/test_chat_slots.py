"""Tests for the chat SlotManager — default champion → zero swaps; distinct
slot models swap exactly once and don't thrash on consecutive same-slot turns."""

from __future__ import annotations

import pytest

from luxe.chat import slots as slots_mod
from luxe.config import ChatSlots, PipelineConfig, RoleConfig, SlotConfig


class FakeBackend:
    def __init__(self, base_url="", model="", timeout_s=600.0, api_key=""):
        self.base_url = base_url
        self.model = model
        self.timeout_s = timeout_s
        self.api_key = api_key
        self.unload_calls: list = []
        self.thermal_calls: list = []

    def unload_all_loaded(self, *, except_for=None):
        self.unload_calls.append(except_for)
        return {}

    def thermal_guard(self, target_model, settle_s=2.0, max_wait_s=30.0):
        self.thermal_calls.append(target_model)
        return True


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    monkeypatch.setattr(slots_mod, "Backend", FakeBackend)


def _champion_cfg() -> PipelineConfig:
    return PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
    )


def _fanout_cfg() -> PipelineConfig:
    return PipelineConfig(
        models={"monolith": "Champ", "coder": "Coder"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        slots=ChatSlots(code=SlotConfig(model_key="coder")),
    )


def test_default_champion_never_swaps():
    sm = slots_mod.SlotManager(_champion_cfg())
    for slot in ("chat", "plan", "code", "chat", "code"):
        b = sm.backend_for(slot)
        assert b.model == "Champ"
    assert sm.stats.count == 0
    assert sm.resident == "Champ"


def test_distinct_code_model_swaps_once():
    sm = slots_mod.SlotManager(_fanout_cfg())
    assert sm.resident == "Champ"  # chat-slot model resident at start
    sm.backend_for("chat")
    assert sm.stats.count == 0  # no swap for the resident model

    sm.backend_for("code")  # → Coder, one swap
    assert sm.stats.count == 1
    assert sm.resident == "Coder"
    assert sm.backend.thermal_calls == ["Coder"]
    assert sm.backend.unload_calls == [["Coder"]]  # except_for the target


def test_consecutive_code_turns_do_not_rethrash():
    sm = slots_mod.SlotManager(_fanout_cfg())
    sm.backend_for("code")
    sm.backend_for("code")
    sm.backend_for("code")
    assert sm.stats.count == 1  # only the first triggered a swap


def test_switching_back_to_chat_swaps_again():
    sm = slots_mod.SlotManager(_fanout_cfg())
    sm.backend_for("code")   # swap 1 → Coder
    sm.backend_for("chat")   # swap 2 → Champ
    assert sm.stats.count == 2
    assert sm.resident == "Champ"


def test_override_repoints_slot():
    sm = slots_mod.SlotManager(_champion_cfg())
    sm.cfg.models["other"] = "Other-Model"
    sm.set_override("plan", "Other-Model")
    assert sm.model_for("plan") == "Other-Model"
    sm.backend_for("plan")
    assert sm.stats.count == 1
    assert sm.resident == "Other-Model"


def test_unknown_slot_raises():
    sm = slots_mod.SlotManager(_champion_cfg())
    with pytest.raises(KeyError):
        sm.model_for("planner")


# --- auto-degrade (host-manifest fallback) ----------------------------------


class ManifestBackend(FakeBackend):
    """FakeBackend with a catalog + health, for the degrade paths."""

    served: list[str] = ["Main-M", "Fb-M"]
    healthy = True
    guard_ok = True

    def list_models(self):
        return list(self.served)

    def health(self):
        return self.healthy

    def thermal_guard(self, target_model, settle_s=2.0, max_wait_s=30.0):
        self.thermal_calls.append(target_model)
        return self.guard_ok


def _manifest_cfg(monkeypatch) -> PipelineConfig:
    import luxe.config as config_mod

    from luxe.config import HostManifest

    monkeypatch.setattr(config_mod, "short_hostname", lambda: "here")
    return PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        hosts={"here": HostManifest(main="Main-M", fallback="Fb-M")},
    )


def test_manifest_main_drives_slots_and_no_degrade_when_served(monkeypatch):
    monkeypatch.setattr(slots_mod, "Backend", ManifestBackend)
    ManifestBackend.served = ["Main-M", "Fb-M"]
    sm = slots_mod.SlotManager(_manifest_cfg(monkeypatch))
    assert sm.resident == "Main-M"
    b = sm.backend_for("chat")
    assert b.model == "Main-M"
    assert sm.degraded_from is None


def test_catalog_miss_degrades_loudly(monkeypatch):
    monkeypatch.setattr(slots_mod, "Backend", ManifestBackend)
    ManifestBackend.served = ["Fb-M"]          # main vanished from the catalog
    notices: list[str] = []
    sm = slots_mod.SlotManager(_manifest_cfg(monkeypatch),
                               on_status=notices.append)
    b = sm.backend_for("chat")
    assert b.model == "Fb-M"
    assert sm.degraded_from == "Main-M" and sm.degraded_to == "Fb-M"
    assert any("DEGRADED" in n for n in notices)
    # Every slot now reroutes; a manual override would still win.
    assert sm.model_for("plan") == "Fb-M"
    sm.set_override("plan", "Main-M")
    assert sm.model_for("plan") == "Main-M"


def test_failed_swap_guard_degrades_to_fallback(monkeypatch):
    monkeypatch.setattr(slots_mod, "Backend", ManifestBackend)
    ManifestBackend.served = ["Main-M", "Fb-M"]
    ManifestBackend.guard_ok = False           # weights never come up
    try:
        notices: list[str] = []
        sm = slots_mod.SlotManager(_manifest_cfg(monkeypatch),
                                   on_status=notices.append)
        sm._resident = ""                      # force a swap on next turn
        sm.backend_for("chat")
        assert sm.degraded_to == "Fb-M"
        assert any("DEGRADED" in n for n in notices)
    finally:
        ManifestBackend.guard_ok = True


def test_turn_failure_on_healthy_endpoint_degrades(monkeypatch):
    monkeypatch.setattr(slots_mod, "Backend", ManifestBackend)
    ManifestBackend.served = ["Main-M", "Fb-M"]
    sm = slots_mod.SlotManager(_manifest_cfg(monkeypatch))
    sm.backend.model = "Main-M"
    notice = sm.note_turn_failure()
    assert notice and "Fb-M" in notice
    assert sm.degraded_to == "Fb-M"
    # Second failure doesn't re-fire (already degraded).
    assert sm.note_turn_failure() is None


def test_ctx_ceiling_clamps_per_model(monkeypatch):
    """The manifest's ctx_max keys on the model the slot CURRENTLY resolves
    to — main uncapped, fallback capped, and a degrade applies the fallback's
    cap automatically."""
    import luxe.config as config_mod

    from luxe.config import HostManifest, RoleConfig

    monkeypatch.setattr(slots_mod, "Backend", ManifestBackend)
    ManifestBackend.served = ["Main-M", "Fb-M"]
    monkeypatch.setattr(config_mod, "short_hostname", lambda: "here")
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith", num_ctx=32768,
                                      num_ctx_max=262144)},
        hosts={"here": HostManifest(main="Main-M", fallback="Fb-M",
                                    ctx_max={"Fb-M": 32768,
                                             "Big-M": 999999999})},
    )
    sm = slots_mod.SlotManager(cfg)
    assert sm.ctx_ceiling("chat") == 262144      # main: role ceiling only

    sm._degrade("test")                          # now resolving to Fb-M
    assert sm.ctx_ceiling("chat") == 32768       # fallback's cap applies

    sm.set_override("chat", "Big-M")             # manual pick, huge cap
    assert sm.ctx_ceiling("chat") == 262144      # role ceiling still wins


def test_turn_failure_on_dead_endpoint_does_not_degrade(monkeypatch):
    monkeypatch.setattr(slots_mod, "Backend", ManifestBackend)
    ManifestBackend.served = ["Main-M", "Fb-M"]
    ManifestBackend.healthy = False
    try:
        sm = slots_mod.SlotManager(_manifest_cfg(monkeypatch))
        sm.backend.model = "Main-M"
        assert sm.note_turn_failure() is None   # endpoint problem, not model
        assert sm.degraded_from is None
    finally:
        ManifestBackend.healthy = True
