"""Tests for the chat SlotManager — default champion → zero swaps; distinct
slot models swap exactly once and don't thrash on consecutive same-slot turns."""

from __future__ import annotations

import pytest

from luxe.chat import slots as slots_mod
from luxe.config import ChatSlots, PipelineConfig, RoleConfig, SlotConfig


class FakeBackend:
    def __init__(self, base_url="", model=""):
        self.base_url = base_url
        self.model = model
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
