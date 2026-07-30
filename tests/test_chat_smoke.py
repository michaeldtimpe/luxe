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


def test_empty_response_is_the_deleted_weights_signature():
    _Backend.reply_text = ""
    report = smoke_mod.run_smoke(_cfg(), skip_fallback=True)
    step = next(s for s in report.steps if s.name == "main turn")
    assert step.state == "fail" and "deleted weights" in step.detail


def test_missing_tool_call_fails():
    _Backend.tool_reply = []
    report = smoke_mod.run_smoke(_cfg(), skip_fallback=True)
    assert _states(report)["tool call"] == "fail"


def test_fallback_leg_failure_is_reported():
    _Backend.fail_models = {"Fb-M"}
    report = smoke_mod.run_smoke(_cfg())
    step = next(s for s in report.steps if s.name == "fallback turn")
    assert step.state == "fail" and "exploded" in step.detail


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
