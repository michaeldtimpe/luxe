"""`/steps`, `/maxtokens`, `/loopguard` — per-session agent-loop limits.

Three layers: the loop honours `RoleConfig.stuck_loop_abort` (default True ⇒
unchanged), `prepare_turn` applies the session overrides to the turn's role
(nothing typed ⇒ the role's own values), and the commands set/reset them.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from luxe.agents.loop import run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.chat import commands as cmd
from luxe.chat import repl
from luxe.chat import slots as slots_mod
from luxe.chat.session import ChatSession
from luxe.config import PipelineConfig, RoleConfig
from luxe.tools.base import ToolDef
from tests._fakes import ScriptedBackend


# -- loop: stuck_loop_abort ---------------------------------------------------

def _same_call() -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="c", name="grep",
                                     arguments={"pattern": "foo"})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=50),
    )


def _done() -> ChatResponse:
    return ChatResponse(text="done", finish_reason="stop",
                        timing=GenerationTiming(prompt_tokens=100,
                                                completion_tokens=10))


def _run(role: RoleConfig, scripted):
    tool = ToolDef(name="grep", description="grep",
                   parameters={"type": "object",
                               "properties": {"pattern": {"type": "string"}},
                               "required": ["pattern"]})
    return run_agent(backend=ScriptedBackend(scripted), role_cfg=role,
                     system_prompt="sys", task_prompt="go",
                     tool_defs=[tool],
                     tool_fns={"grep": lambda a: ("hit", None)})


def _role(**kw) -> RoleConfig:
    return RoleConfig(model_key="t", num_ctx=4096, max_steps=10,
                      max_tokens_per_turn=2048, temperature=0.0, **kw)


def test_guard_on_by_default_aborts_a_repeat_streak():
    result = _run(_role(), [_same_call() for _ in range(5)] + [_done()])
    assert result.aborted
    assert result.abort_reason.startswith("Stuck in loop")


def test_guard_off_keeps_going_and_finishes():
    result = _run(_role(stuck_loop_abort=False),
                  [_same_call() for _ in range(5)] + [_done()])
    assert not result.aborted
    assert result.final_text == "done"


def test_guard_off_is_still_bounded_by_max_steps():
    result = _run(_role(stuck_loop_abort=False),
                  [_same_call() for _ in range(20)])
    assert result.aborted
    assert result.abort_reason == "Max steps reached (10)"


# -- prepare_turn: overrides reach the role -----------------------------------

class _FakeBackend:
    def __init__(self, base_url="", model="", timeout_s=600.0, api_key="", **kw):
        self.base_url, self.model = base_url, model

    def unload_all_loaded(self, *, except_for=None):
        return {}

    def thermal_guard(self, *a, **k):
        return True

    def health(self):
        return True

    def list_models(self):
        return []


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(slots_mod, "Backend", _FakeBackend)


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith", max_steps=30,
                                      max_tokens_per_turn=8192)},
    )
    return cfg, ChatSession(repo_path=str(repo)), slots_mod.SlotManager(cfg)


def _prep(setup):
    cfg, session, sm = setup
    return repl.prepare_turn("hello", session, sm, cfg, frozenset(),
                             lambda m: "review")


def test_no_overrides_leave_the_role_untouched(setup):
    role = _prep(setup).role_cfg
    assert (role.max_steps, role.max_tokens_per_turn, role.stuck_loop_abort) \
        == (30, 8192, True)


def test_overrides_apply_to_the_turn(setup):
    _, session, sm = setup
    session.max_steps_override = 60
    session.max_tokens_override = 16384
    session.loop_guard = False
    role = _prep(setup).role_cfg
    assert (role.max_steps, role.max_tokens_per_turn, role.stuck_loop_abort) \
        == (60, 16384, False)
    assert sm.role_for("chat").max_steps == 30          # config role untouched


# -- commands ------------------------------------------------------------------

@pytest.fixture
def ctx(setup):
    cfg, session, sm = setup
    out = io.StringIO()
    c = cmd.CommandContext(console=Console(file=out, force_terminal=False,
                                           width=120),
                           session=session, slots=sm)
    c._out = out  # type: ignore[attr-defined]
    return c


def test_steps_show_set_reset(ctx):
    cmd.dispatch("/steps", ctx)
    assert "30" in ctx._out.getvalue()
    cmd.dispatch("/steps 60", ctx)
    assert ctx.session.max_steps_override == 60
    cmd.dispatch("/steps default", ctx)
    assert ctx.session.max_steps_override is None


def test_steps_rejects_out_of_range_and_junk(ctx):
    for bad in ("0", "9999", "lots"):
        cmd.dispatch(f"/steps {bad}", ctx)
        assert ctx.session.max_steps_override is None
    assert "Expected a number" in ctx._out.getvalue()


def test_maxtokens_accepts_k_suffix_and_alias(ctx):
    cmd.dispatch("/maxtokens 16k", ctx)
    assert ctx.session.max_tokens_override == 16000
    cmd.dispatch("/max-tokens 32768", ctx)
    assert ctx.session.max_tokens_override == 32768
    cmd.dispatch("/maxtokens 100", ctx)                  # below the floor
    assert ctx.session.max_tokens_override == 32768


def test_loopguard_toggle(ctx):
    assert ctx.session.loop_guard is True
    cmd.dispatch("/loopguard off", ctx)
    assert ctx.session.loop_guard is False
    cmd.dispatch("/loopguard on", ctx)
    assert ctx.session.loop_guard is True
    cmd.dispatch("/loopguard maybe", ctx)
    assert "Usage" in ctx._out.getvalue()


def test_frontier_sets_and_resets_all_three(ctx):
    from luxe.chat.session import frontier_active
    cmd.dispatch("/frontier", ctx)
    s = ctx.session
    assert (s.max_steps_override, s.max_tokens_override, s.loop_guard) \
        == (500, 16384, False)
    assert frontier_active(s)
    cmd.dispatch("/steps 200", ctx)                      # tweak one: no longer the preset
    assert not frontier_active(s)
    cmd.dispatch("/frontier off", ctx)
    assert (s.max_steps_override, s.max_tokens_override, s.loop_guard) \
        == (None, None, True)
    cmd.dispatch("/frontier sideways", ctx)
    assert "Usage" in ctx._out.getvalue()


def test_frontier_preset_reaches_the_turn(setup):
    _, session, _ = setup
    session.max_steps_override, session.max_tokens_override = 500, 16384
    session.loop_guard = False
    role = _prep(setup).role_cfg
    assert (role.max_steps, role.max_tokens_per_turn, role.stuck_loop_abort) \
        == (500, 16384, False)


# -- status bar ----------------------------------------------------------------

def _bar(setup) -> str:
    from luxe.chat import status
    _, session, sm = setup
    segs = status.fields(session, sm, session.repo_path, status.StatusState())
    return " · ".join(status._seg_text(s) for s in segs)


def test_status_bar_hides_limits_at_defaults(setup):
    bar = _bar(setup)
    assert "limits " not in bar and "frontier " not in bar


def test_status_bar_shows_overrides_and_frontier(setup):
    _, session, _ = setup
    session.max_steps_override = 60
    assert "limits steps 60" in _bar(setup)
    session.max_steps_override, session.max_tokens_override = 500, 16384
    session.loop_guard = False
    assert "frontier steps 500 tok 16k loopguard off" in _bar(setup)


def test_help_names_frontier_and_openrouter(ctx):
    cmd.dispatch("/help", ctx)
    out = ctx._out.getvalue()
    assert "/frontier" in out
    assert "--backend openrouter" in out and "/model find" in out
