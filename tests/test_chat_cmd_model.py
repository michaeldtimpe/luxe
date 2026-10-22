"""Characterization of `/model` (`cmd_models._model`) — exact output + state.

Written 2026-10-22 ahead of decomposing `_model` (C901 25) so the refactor
is provably behaviour-identical: every branch the handler takes is pinned to
its exact printed lines, the resulting slot overrides, and the order in which
it consults the slot manager / origin / modelcaps. `/model find` is covered
in test_chat_backends.py; here only its dispatch from `_model` is pinned.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from luxe.chat import cmd_models
from luxe.chat import commands as cmd
from luxe.chat import modelcaps
from luxe.chat import origin as origin_mod
from luxe.chat import slots as slots_mod
from luxe.chat.session import ChatSession
from luxe.config import PipelineConfig, RoleConfig


class _Backend:
    def __init__(self, base_url="", model="", timeout_s=600.0, api_key="",
                 body_extras=None, key_fallback=True, send_num_ctx=True):
        self.base_url = base_url
        self.model = model

    def health(self):
        return True

    def list_models(self):
        return []


@pytest.fixture(autouse=True)
def _iso(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(slots_mod, "Backend", _Backend)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _remote(glyph="⇅", label="remote", describe="served by m5 over the tailnet"):
    return SimpleNamespace(is_over_the_network=True, glyph=glyph, label=label,
                           describe=lambda: describe)


_LOCAL = SimpleNamespace(is_over_the_network=False, glyph="⌂", label="local",
                         describe=lambda: "on this disk")
_NO_TOOLS = modelcaps.ToolCapability(modelcaps.UNSUPPORTED,
                                     "its chat template has no tool markers")
_TOOLS = modelcaps.ToolCapability(modelcaps.SUPPORTED, "")


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch):
    cfg = PipelineConfig(models={"monolith": "Champ"},
                         roles={"monolith": RoleConfig(model_key="monolith")})
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=200)
    sm = slots_mod.SlotManager(cfg)
    c = cmd.CommandContext(console=console, session=ChatSession(repo_path=str(tmp_path)),
                           slots=sm)
    c._out = out  # type: ignore[attr-defined]
    # Deterministic collaborators; each test overrides what it exercises.
    monkeypatch.setattr(sm, "available_models", lambda: [])
    monkeypatch.setattr(sm, "catalog_is_larger_than_roster", lambda: False)
    monkeypatch.setattr(sm, "engine_label", lambda: "oMLX")
    monkeypatch.setattr(origin_mod, "origins_for_backend", lambda b: {})
    monkeypatch.setattr(origin_mod, "origin_for", lambda b, m: None)
    monkeypatch.setattr(modelcaps, "for_model", lambda b, m: _TOOLS)
    return c


def _lines(ctx) -> list[str]:
    return ctx._out.getvalue().splitlines()


def _overrides(ctx) -> dict:
    return dict(ctx.slots.overrides)


# --- `/model` (no args): the roster -----------------------------------------


def test_list_with_roster_marks_resident_in_use_remote_and_toolless(ctx, monkeypatch):
    sm = ctx.slots
    sm._resident = "M-a"
    sm.set_override("code", "M-b")
    monkeypatch.setattr(sm, "available_models", lambda: ["M-a", "M-b", "M-c", "Champ"])
    monkeypatch.setattr(origin_mod, "origins_for_backend",
                        lambda b: {"M-a": _LOCAL, "M-c": _remote()})
    monkeypatch.setattr(modelcaps, "for_model",
                        lambda b, m: _NO_TOOLS if m == "M-c" else _TOOLS)
    res = cmd.dispatch("/model", ctx)
    assert res.handled and not res.exit
    assert _lines(ctx) == [
        "Model slots (resident in RAM: M-a)",
        "  chat  → Champ",
        "  plan  → Champ",
        "  code  → M-b",
        "available models — `/model <slot> <n>`:",
        "   1 M-a  (resident)",
        "   2 M-b  (in use)",
        "   3 M-c ⇅ remote ⚠ no tool support",
        "   4 Champ  (in use)",
        "  ☁ network volume · ⇅ remote endpoint (unmarked = local disk)",
    ]
    assert _overrides(ctx) == {"code": "M-b"}


def test_list_resident_and_in_use_together(ctx, monkeypatch):
    ctx.slots._resident = "Champ"
    monkeypatch.setattr(ctx.slots, "available_models", lambda: ["Champ"])
    cmd.dispatch("/model", ctx)
    assert _lines(ctx)[-1] == "   1 Champ  (resident, in use)"


def test_list_all_local_has_no_legend(ctx, monkeypatch):
    monkeypatch.setattr(ctx.slots, "available_models", lambda: ["X"])
    monkeypatch.setattr(origin_mod, "origins_for_backend", lambda b: {"X": _LOCAL})
    cmd.dispatch("/model", ctx)
    assert _lines(ctx)[-2:] == ["available models — `/model <slot> <n>`:", "   1 X"]


def test_list_survives_origin_probe_failure(ctx, monkeypatch):
    def boom(b):
        raise OSError("mount timed out")
    monkeypatch.setattr(ctx.slots, "available_models", lambda: ["X"])
    monkeypatch.setattr(origin_mod, "origins_for_backend", boom)
    cmd.dispatch("/model", ctx)
    assert _lines(ctx)[-1] == "   1 X"


def test_list_unreachable_endpoint(ctx):
    cmd.dispatch("/model", ctx)
    assert _lines(ctx)[4:] == ["(oMLX unreachable — `/model <slot> <id>` still works)"]


def test_list_unreachable_names_the_engine(ctx, monkeypatch):
    monkeypatch.setattr(ctx.slots, "engine_label", lambda: "llama-server")
    cmd.dispatch("/model", ctx)
    assert _lines(ctx)[-1] == "(llama-server unreachable — `/model <slot> <id>` still works)"


@pytest.mark.parametrize("avail", [[], ["X"]])
def test_list_points_at_find_when_catalog_is_larger(ctx, monkeypatch, avail):
    monkeypatch.setattr(ctx.slots, "available_models", lambda: avail)
    monkeypatch.setattr(ctx.slots, "catalog_is_larger_than_roster", lambda: True)
    cmd.dispatch("/model", ctx)
    assert _lines(ctx)[-1] == ("· `/model find <text>` searches the full catalog "
                               "(this list is the shortlist)")


def test_list_consults_collaborators_in_order(ctx, monkeypatch):
    calls: list[str] = []
    sm = ctx.slots

    def rec(name, value):
        def f(*a, **k):
            calls.append(name)
            return value(*a) if callable(value) else value
        return f

    real_slot_models = sm.slot_models
    monkeypatch.setattr(sm, "slot_models", rec("slot_models", lambda: real_slot_models()))
    monkeypatch.setattr(sm, "available_models", rec("available_models", ["A", "B"]))
    monkeypatch.setattr(sm, "catalog_is_larger_than_roster", rec("catalog_larger", False))
    monkeypatch.setattr(origin_mod, "origins_for_backend", rec("origins", {}))
    monkeypatch.setattr(modelcaps, "for_model",
                        lambda b, m: (calls.append(f"caps:{m}"), _TOOLS)[1])
    cmd.dispatch("/model", ctx)
    assert calls == ["slot_models", "available_models", "origins",
                     "caps:A", "caps:B", "catalog_larger"]


# --- dispatch: find / unknown slot / bare slot -------------------------------


def test_find_is_delegated_with_the_rest_of_the_args(ctx, monkeypatch):
    seen = []
    monkeypatch.setattr(cmd_models, "_model_find",
                        lambda args, c: (seen.append(list(args)), cmd.CommandResult(handled=True))[1])
    cmd.dispatch("/model find kimi k3", ctx)
    assert seen == [["kimi", "k3"]]
    assert _lines(ctx) == []


def test_unknown_slot(ctx):
    cmd.dispatch("/model bogus Champ", ctx)
    assert _lines(ctx) == [
        "Unknown slot 'bogus'; expected chat|plan|code|all (or `find <text>`).",
    ]
    assert _overrides(ctx) == {}


def test_bare_slot_shows_that_slot(ctx):
    ctx.slots.set_override("plan", "P")
    cmd.dispatch("/model plan", ctx)
    assert _lines(ctx) == ["  plan → P"]


def test_bare_all_shows_every_slot(ctx):
    ctx.slots.set_override("code", "C")
    cmd.dispatch("/model all", ctx)
    assert _lines(ctx) == ["  chat → Champ", "  plan → Champ", "  code → C"]


# --- numeric selection --------------------------------------------------------


def test_numeric_with_no_roster(ctx):
    cmd.dispatch("/model chat 1", ctx)
    assert _lines(ctx) == [
        "No available-model list (oMLX unreachable) — pass an explicit id: "
        "/model <slot> <id>.",
    ]
    assert _overrides(ctx) == {}


@pytest.mark.parametrize("n", ["0", "3"])
def test_numeric_out_of_range(ctx, monkeypatch, n):
    monkeypatch.setattr(ctx.slots, "available_models", lambda: ["A", "B"])
    cmd.dispatch(f"/model code {n}", ctx)
    assert _lines(ctx) == ["Pick 1–2 (see /model)."]
    assert _overrides(ctx) == {}


def test_numeric_pick_sets_slot_and_skips_the_catalog_warning(ctx, monkeypatch):
    monkeypatch.setattr(ctx.slots, "available_models", lambda: ["A", "B"])
    warned = []
    monkeypatch.setattr(cmd_models, "_warn_model_not_offered",
                        lambda c, m: warned.append(m))
    cmd.dispatch("/model code 2", ctx)
    assert _lines(ctx) == ["✓ slot code → B (swaps on next code turn)"]
    assert _overrides(ctx) == {"code": "B"}
    assert warned == []


def test_numeric_pick_all(ctx, monkeypatch):
    monkeypatch.setattr(ctx.slots, "available_models", lambda: ["A", "B"])
    cmd.dispatch("/model all 1", ctx)
    assert _lines(ctx) == ["✓ slots chat·plan·code → A (swaps on next turn)"]
    assert _overrides(ctx) == {"chat": "A", "plan": "A", "code": "A"}


# --- explicit id ----------------------------------------------------------------


def test_explicit_id_warns_before_setting(ctx, monkeypatch):
    order = []
    monkeypatch.setattr(cmd_models, "_warn_model_not_offered",
                        lambda c, m: order.append(("warn", m)))
    real_set = ctx.slots.set_override
    monkeypatch.setattr(ctx.slots, "set_override",
                        lambda s, m: (order.append(("set", s, m)), real_set(s, m))[1])
    monkeypatch.setattr(origin_mod, "origin_for",
                        lambda b, m: (order.append(("origin", m)), None)[1])
    monkeypatch.setattr(modelcaps, "for_model",
                        lambda b, m: (order.append(("caps", m)), _TOOLS)[1])
    cmd.dispatch("/model all Big", ctx)
    assert order == [("warn", "Big"), ("set", "chat", "Big"), ("set", "plan", "Big"),
                     ("set", "code", "Big"), ("origin", "Big"), ("caps", "Big")]
    assert _lines(ctx) == ["✓ slots chat·plan·code → Big (swaps on next turn)"]


def test_explicit_id_hidden_by_roster(ctx, monkeypatch):
    monkeypatch.setattr(ctx.slots, "available_models", lambda: ["Champ"])
    monkeypatch.setattr(type(ctx.slots.backend), "list_models",
                        lambda self: ["Champ", "Secret"])
    cmd.dispatch("/model chat Secret", ctx)
    assert _lines(ctx) == [
        "  · Secret is served here but hidden from the picker by `visible_models:` "
        "in configs/chat.yaml — using it anyway",
        "✓ slot chat → Secret (swaps on next chat turn)",
    ]
    assert _overrides(ctx) == {"chat": "Secret"}


def test_remote_origin_and_toolless_model_are_both_announced(ctx, monkeypatch):
    monkeypatch.setattr(origin_mod, "origin_for",
                        lambda b, m: _remote(describe="streams from m5 (remote endpoint)"))
    monkeypatch.setattr(modelcaps, "for_model", lambda b, m: _NO_TOOLS)
    cmd.dispatch("/model chat gemma-3", ctx)
    assert _lines(ctx) == [
        "✓ slot chat → gemma-3 (swaps on next chat turn)",
        "  ⇅ streams from m5 (remote endpoint)",
        "  ⚠ gemma-3 cannot call tools — its chat template has no tool markers. "
        "luxe will withhold the tool surface on its turns: conversation only, "
        "no reading or editing files.",
    ]
    assert _overrides(ctx) == {"chat": "gemma-3"}


def test_local_origin_is_silent_and_origin_failure_is_swallowed(ctx, monkeypatch):
    monkeypatch.setattr(origin_mod, "origin_for", lambda b, m: _LOCAL)
    cmd.dispatch("/model plan Local", ctx)

    def boom(b, m):
        raise OSError("stat hung")
    monkeypatch.setattr(origin_mod, "origin_for", boom)
    cmd.dispatch("/model plan Other", ctx)
    assert _lines(ctx) == [
        "✓ slot plan → Local (swaps on next plan turn)",
        "✓ slot plan → Other (swaps on next plan turn)",
    ]
    assert _overrides(ctx) == {"plan": "Other"}


def test_reselecting_the_current_model_still_confirms(ctx):
    cmd.dispatch("/model chat Champ", ctx)
    assert _lines(ctx) == ["✓ slot chat → Champ (swaps on next chat turn)"]
    assert _overrides(ctx) == {"chat": "Champ"}
