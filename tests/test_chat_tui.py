"""Textual TUI smoke/behaviour tests (driven via asyncio.run + Pilot, so they
don't depend on pytest-asyncio mode config). run_single is stubbed — no model."""

from __future__ import annotations

import asyncio

import pytest

textual = pytest.importorskip("textual")

from textual.widgets import Input, RichLog  # noqa: E402

from luxe.chat import repl as _repl  # noqa: E402
from luxe.chat import slots as slots_mod  # noqa: E402
from luxe.chat.session import ChatSession  # noqa: E402
from luxe.chat.tui import ChatApp, StatusBar  # noqa: E402
from luxe.config import PipelineConfig, RoleConfig  # noqa: E402
from luxe.memory import session as session_store  # noqa: E402


class _FakeBackend:
    def __init__(self, base_url="", model=""):
        self.model = model

    def unload_all_loaded(self, *, except_for=None):
        return {}

    def thermal_guard(self, *a, **k):
        return True


class _FakeResult:
    final_text = "**Hello** from the model"
    steps = 1
    tool_calls_total = 0
    wall_s = 0.5
    completion_tokens = 10
    prompt_tokens = 20
    peak_context_pressure = 0.1
    final_context_pressure = 0.1


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(slots_mod, "Backend", _FakeBackend)


def _make_app(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = PipelineConfig(models={"monolith": "Champ"},
                         roles={"monolith": RoleConfig(model_key="monolith")})
    session = ChatSession(repo_path=str(repo))
    meta = session_store.new_session(repo_path=str(repo), project_hash="h", slot_models={})
    session.session_id = meta.session_id
    return ChatApp(cfg, str(repo), frozenset(), session=session,
                   slots=slots_mod.SlotManager(cfg), infer=lambda m: "review",
                   keep_loaded=True)


def test_boots(tmp_path):
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#transcript", RichLog) is not None
            assert app.query_one("#prompt", Input) is not None
            assert app.query_one("#status", StatusBar) is not None
    asyncio.run(scenario())


def test_turn_renders_final(tmp_path, monkeypatch):
    monkeypatch.setattr(_repl, "run_single", lambda *a, **k: _FakeResult())

    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt", Input).value = "hi there"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            # the turn ran through the core and persisted to history
            assert app.session.turns
            assert app.session.turns[-1].assistant == "**Hello** from the model"
            assert not app._busy
    asyncio.run(scenario())


def test_command_dispatch(tmp_path):
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt", Input).value = "/compact"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.session.compact is True
    asyncio.run(scenario())


def test_prompt_user_requires_worker_thread(tmp_path):
    async def scenario():
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # calling from the UI/main thread must raise (deadlock guard)
            with pytest.raises(AssertionError):
                app.prompt_user("pick?")
    asyncio.run(scenario())
