"""Tests for `luxe smoke` — the fallback-kit aliveness drill (chat/smoke.py).

All fake-backend: the drill's real generations are exercised on hosts via
`luxe smoke` itself; here we pin the verdict logic (manifest gate, dangling
weights, empty-response detection, tool-call detection, fallback leg)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

import luxe.chat.smoke as smoke_mod
from luxe.backend import BackendError
from luxe.config import HostManifest, PipelineConfig, RoleConfig


@dataclass
class _Resp:
    text: str = "OK"
    tool_calls: list = field(default_factory=list)


@dataclass
class _ToolCall:
    name: str = "read_file"


class _Backend:
    served = ["Main-M", "Fb-M"]
    healthy = True
    reply_text = "OK"
    tool_reply: list = [_ToolCall()]
    fail_models: set = set()

    def __init__(self, base_url="", model="", timeout_s=600.0, api_key=""):
        self.base_url = base_url
        self.model = model
        self.chatted: list[str] = []

    def health(self):
        return self.healthy

    def list_models(self):
        return list(self.served)

    def unload_all_loaded(self, *, except_for=None):
        return {}

    def chat(self, messages, tools=None, max_tokens=2048, temperature=0.2,
             **kw):
        if self.model in self.fail_models:
            raise BackendError(f"{self.model} exploded")
        self.chatted.append(self.model)
        if tools:
            return _Resp(text="", tool_calls=list(self.tool_reply))
        return _Resp(text=self.reply_text)


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    import luxe.config as config_mod
    from luxe.chat import origin as origin_mod

    monkeypatch.setattr(smoke_mod, "Backend", _Backend)
    monkeypatch.setattr(config_mod, "short_hostname", lambda: "here")
    monkeypatch.setattr(origin_mod, "endpoint_is_local", lambda url: False)
    _Backend.served = ["Main-M", "Fb-M"]
    _Backend.healthy = True
    _Backend.reply_text = "OK"
    _Backend.tool_reply = [_ToolCall()]
    _Backend.fail_models = set()
    yield


def _cfg(hosts=None) -> PipelineConfig:
    return PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        hosts=hosts if hosts is not None else {
            "here": HostManifest(main="Main-M", fallback="Fb-M")},
    )


def _states(report) -> dict[str, str]:
    return {s.name: s.state for s in report.steps}


def test_happy_path_is_ready():
    report = smoke_mod.run_smoke(_cfg())
    states = _states(report)
    assert not report.failed
    assert states["manifest"] == "pass"
    assert states["endpoint"] == "pass"
    assert states["catalog"] == "pass"
    assert states["main turn"] == "pass"
    assert states["tool call"] == "pass"
    assert states["fallback turn"] == "pass"


def test_unmatched_host_fails_fast():
    report = smoke_mod.run_smoke(_cfg(hosts={"m9": HostManifest(main="X")}))
    assert report.failed
    assert _states(report)["manifest"] == "fail"
    assert len(report.steps) == 1  # nothing else runs


def test_dead_endpoint_fails_before_generations():
    _Backend.healthy = False
    report = smoke_mod.run_smoke(_cfg())
    assert report.failed
    assert _states(report)["endpoint"] == "fail"
    assert "main turn" not in _states(report)


def test_empty_response_states_observable_and_discriminator():
    _Backend.reply_text = ""
    report = smoke_mod.run_smoke(_cfg(), skip_fallback=True)
    step = next(s for s in report.steps if s.name == "main turn")
    # Observable-first hint (2026-08-03): names the symptom and the
    # completion_tokens discriminator, not a single theory.
    assert step.state == "fail"
    assert "empty response" in step.detail
    assert "completion_tokens=" in step.detail
    assert "dangling weights" in step.detail


def test_missing_tool_call_fails():
    _Backend.tool_reply = []
    report = smoke_mod.run_smoke(_cfg(), skip_fallback=True)
    assert _states(report)["tool call"] == "fail"


def test_fallback_leg_failure_is_reported():
    _Backend.fail_models = {"Fb-M"}
    report = smoke_mod.run_smoke(_cfg())
    step = next(s for s in report.steps if s.name == "fallback turn")
    assert step.state == "fail" and "exploded" in step.detail


# --- code / chat drills ------------------------------------------------------


def _drill_cfg():
    from luxe.config import RoleConfig as RC

    return PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RC(model_key="monolith",
                              tools=["read_file", "list_dir", "grep",
                                     "bm25_search", "write_file", "edit_file",
                                     "bash"])},
        task_types={},
        hosts={"here": HostManifest(main="Main-M", fallback="Fb-M")},
    )


class _FakeResult:
    def __init__(self, text="", steps=3, tool_calls_total=4):
        self.final_text = text
        self.steps = steps
        self.tool_calls_total = tool_calls_total


def _fix_calc(repo):
    calc = repo / "calc.py"
    calc.write_text(calc.read_text().replace(
        "return a - b  # planted bug", "return a + b"))


def test_code_drill_passes_when_agent_fixes_the_bug(monkeypatch, tmp_path):
    import luxe.agents.single as single_mod
    from luxe.tools import fs as fs_mod

    def fake_run_single(backend, role, *, goal, task_type, run_id=None, **kw):
        _fix_calc(fs_mod.get_repo_root())
        return _FakeResult()

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    report = smoke_mod.run_code_drill(_drill_cfg())
    states = _states(report)
    assert not report.failed
    assert states["code agent"] == "pass"
    assert states["tests"] == "pass"
    assert states["diff"] == "pass"
    assert "kept" not in states          # scratch repo cleaned up on success


def test_code_drill_fails_when_agent_does_nothing(monkeypatch):
    import shutil

    import luxe.agents.single as single_mod

    monkeypatch.setattr(single_mod, "run_single",
                        lambda *a, **k: _FakeResult())
    report = smoke_mod.run_code_drill(_drill_cfg())
    states = _states(report)
    assert report.failed
    assert states["tests"] == "fail"
    kept = next(s for s in report.steps if s.name == "kept")
    shutil.rmtree(kept.detail.split(": ", 1)[1], ignore_errors=True)


def test_code_drill_requires_tool_calls(monkeypatch):
    import shutil

    import luxe.agents.single as single_mod
    from luxe.tools import fs as fs_mod

    def fake_run_single(backend, role, *, goal, task_type, run_id=None, **kw):
        _fix_calc(fs_mod.get_repo_root())      # right answer, wrong path
        return _FakeResult(tool_calls_total=0)

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    report = smoke_mod.run_code_drill(_drill_cfg())
    assert _states(report)["tool use"] == "fail"
    kept = next(s for s in report.steps if s.name == "kept")
    shutil.rmtree(kept.detail.split(": ", 1)[1], ignore_errors=True)


def test_chat_drill_verifies_the_magic_word(monkeypatch):
    import shutil

    import luxe.agents.single as single_mod

    monkeypatch.setattr(
        single_mod, "run_single",
        lambda *a, **k: _FakeResult(text=f"It is {smoke_mod._DRILL_MAGIC}."))
    report = smoke_mod.run_chat_drill(_drill_cfg())
    assert not report.failed
    assert _states(report)["answer"] == "pass"

    monkeypatch.setattr(single_mod, "run_single",
                        lambda *a, **k: _FakeResult(text="No idea, sorry."))
    report = smoke_mod.run_chat_drill(_drill_cfg())
    assert _states(report)["answer"] == "fail"
    kept = next(s for s in report.steps if s.name == "kept")
    shutil.rmtree(kept.detail.split(": ", 1)[1], ignore_errors=True)


def test_chat_drill_strips_the_write_surface(monkeypatch):
    import shutil

    import luxe.agents.single as single_mod

    seen = {}

    def fake_run_single(backend, role, *, goal, task_type, run_id=None, **kw):
        seen["tools"] = list(role.tools)
        seen["model"] = backend.model
        return _FakeResult(text=smoke_mod._DRILL_MAGIC)

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    report = smoke_mod.run_chat_drill(_drill_cfg())
    assert not report.failed
    assert "write_file" not in seen["tools"]
    assert "bash" not in seen["tools"]
    assert "bm25_search" not in seen["tools"]   # no index in a drill repo
    assert seen["model"] == "Main-M"            # this host's manifest main


def test_drill_backend_resolves_remote_manifest(monkeypatch):
    """A drill pointed at the m5 must run an m5 model, not this host's."""
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        hosts={"here": HostManifest(main="Local-M", fallback="Lf"),
               "m5": HostManifest(main="Remote-M", fallback="Rf")},
    )
    backend, model = smoke_mod._resolve_drill_backend(
        cfg, None, "http://m5.tailnet.example.ts.net:8000")
    assert model == "Remote-M"
    backend, model = smoke_mod._resolve_drill_backend(cfg, None, None)
    assert model == "Local-M"


def test_drill_backend_model_override_beats_the_manifest(monkeypatch):
    """`--model` drills a specific cached model (e.g. the m5 capacity model,
    which is a keep:, never a main) — it must beat manifest resolution."""
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        hosts={"here": HostManifest(main="Local-M", fallback="Lf")},
    )
    backend, model = smoke_mod._resolve_drill_backend(
        cfg, None, None, "Capacity-M")
    assert model == "Capacity-M"
    assert backend.model == "Capacity-M"


def test_dangling_main_weights_fail_on_local_endpoint(monkeypatch):
    import luxe.modelstore as ms
    from luxe.chat import origin as origin_mod

    monkeypatch.setattr(origin_mod, "endpoint_is_local", lambda url: True)
    monkeypatch.setattr(ms, "model_state",
                        lambda mid, *a, **k: "dangling" if mid == "Main-M"
                        else "ok")
    report = smoke_mod.run_smoke(_cfg(), skip_fallback=True, skip_tools=True)
    step = next(s for s in report.steps if s.name == "weights Main-M")
    assert step.state == "fail" and "pull" in step.detail


# --- code-drill step budget (2026-08-10) ------------------------------------

class TestCodeDrillStepBudget:
    """Low-bit quants need headroom to CONCLUDE, not to solve.

    m1 (Qwen3.6-35B-A3B-4bit) landed the correct fix at step 4 of the code
    drill, then made seven identical read_file calls into the 12-step cap, so
    the drill reported `aborted` and never ran its own tests/diff assertions.
    The 6-bit on the same host passed in 6 steps.
    """

    def test_six_bit_champion_keeps_the_calibrated_budget(self):
        from luxe.chat.smoke import _CODE_DRILL_STEPS, _code_drill_steps
        assert _code_drill_steps("Qwen3.6-35B-A3B-6bit") == _CODE_DRILL_STEPS

    def test_four_bit_main_gets_headroom(self):
        from luxe.chat.smoke import (_CODE_DRILL_STEPS,
                                     _CODE_DRILL_STEPS_LOW_BIT,
                                     _code_drill_steps)
        assert _code_drill_steps("Qwen3.6-35B-A3B-4bit") == _CODE_DRILL_STEPS_LOW_BIT
        assert _CODE_DRILL_STEPS_LOW_BIT > _CODE_DRILL_STEPS

    @pytest.mark.parametrize("model", [
        "Qwen3.6-27B-4bit", "GLM-4.5-Air-4bit",
        "some-model-3bit", "some-model-2bit",
        "QWEN3.6-35B-A3B-4BIT",          # case-insensitive
    ])
    def test_every_low_bit_marker_is_recognised(self, model):
        from luxe.chat.smoke import _CODE_DRILL_STEPS_LOW_BIT, _code_drill_steps
        assert _code_drill_steps(model) == _CODE_DRILL_STEPS_LOW_BIT

    @pytest.mark.parametrize("model", [
        "Qwen3.6-35B-A3B-6bit", "Qwen3.6-27B-6bit", "model-8bit", "bf16-model",
    ])
    def test_high_bit_models_are_not_widened(self, model):
        from luxe.chat.smoke import _CODE_DRILL_STEPS, _code_drill_steps
        assert _code_drill_steps(model) == _CODE_DRILL_STEPS

    def test_missing_model_name_falls_back_to_the_base_budget(self):
        from luxe.chat.smoke import _CODE_DRILL_STEPS, _code_drill_steps
        assert _code_drill_steps("") == _CODE_DRILL_STEPS
        assert _code_drill_steps(None) == _CODE_DRILL_STEPS
