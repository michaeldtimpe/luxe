"""Tests for chat/modelcaps.py + the roster filter + the calmer palette.

The load-bearing finding (2026-07-30): gemma-3's chat template handles
system/user/assistant only and has no `tools` block, and oMLX does NOT reject a
request carrying `tools` for such a model — it silently drops them. Verified
against the live server with gemma-3-1b: prose came back, `prompt_tokens=16`,
no tool_calls. An agentic turn on that model can never call a tool and never
says why, so luxe withholds the tool surface up front.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from luxe.chat import modelcaps
from luxe.chat.session import ChatSession
from luxe.config import PipelineConfig, RoleConfig

# The real gemma-3 template shape: three roles, an alternation guard, no tools.
GEMMA_TEMPLATE = """{{ bos_token }}
{%- if messages[0]['role'] == 'system' -%}{%- endif -%}
{%- for message in loop_messages -%}
  {%- if (message['role'] == 'user') != (loop.index0 % 2 == 0) -%}
    {{ raise_exception("Conversation roles must alternate user/assistant/...") }}
  {%- endif -%}
  {%- if message['role'] == 'assistant' -%}{%- endif -%}
{%- endfor -%}"""

QWEN_TEMPLATE = """{%- if tools %}
  {{- '<|im_start|>system\\n' }}
  {%- for tool in tools %}{{- tool | tojson }}{%- endfor %}
{%- endif %}
{%- for message in messages %}
  {%- if message.tool_calls %}{{- message.tool_calls[0].function.name }}{%- endif %}
{%- endfor %}"""


def _model_dir(root: Path, name: str, template: str | None,
               *, as_json: bool = True) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text("{}")
    if template is None:
        return d
    if as_json:
        (d / "chat_template.json").write_text(json.dumps({"chat_template": template}))
    else:
        (d / "chat_template.jinja").write_text(template)
    return d


class _Backend:
    def __init__(self, paths, base_url="http://127.0.0.1:8000"):
        self.base_url = base_url
        self._paths = paths

    def model_paths(self):
        return dict(self._paths)

    def list_models(self):
        return list(self._paths)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from luxe.chat import origin as origin_mod

    modelcaps.reset_cache()
    origin_mod.reset_cache()
    monkeypatch.setattr(origin_mod, "network_mounts", lambda **k: [])
    yield
    modelcaps.reset_cache()
    origin_mod.reset_cache()


# --- template classification ------------------------------------------------


class TestFromTemplate:
    def test_gemma_style_template_is_unsupported(self, tmp_path):
        d = _model_dir(tmp_path, "gemma", GEMMA_TEMPLATE)
        cap = modelcaps.from_template(d)
        assert cap.state == modelcaps.UNSUPPORTED
        assert cap.usable is False
        assert "no tool support" in cap.reason

    def test_qwen_style_template_is_supported(self, tmp_path):
        d = _model_dir(tmp_path, "qwen", QWEN_TEMPLATE)
        cap = modelcaps.from_template(d)
        assert cap.state == modelcaps.SUPPORTED
        assert cap.usable is True

    def test_jinja_file_is_read_too(self, tmp_path):
        d = _model_dir(tmp_path, "qwen2", QWEN_TEMPLATE, as_json=False)
        assert modelcaps.from_template(d).state == modelcaps.SUPPORTED

    def test_missing_template_is_unknown_and_still_usable(self, tmp_path):
        d = _model_dir(tmp_path, "bare", None)
        cap = modelcaps.from_template(d)
        assert cap.state == modelcaps.UNKNOWN
        assert cap.usable is True      # never disarm the agent on a detection gap

    def test_missing_directory_is_unknown(self, tmp_path):
        assert modelcaps.from_template(tmp_path / "nope").state == modelcaps.UNKNOWN


# --- per-endpoint resolution -------------------------------------------------


class TestForModel:
    def test_classifies_and_caches(self, tmp_path):
        gem = _model_dir(tmp_path, "gemma-3-27b-it-4bit", GEMMA_TEMPLATE)
        qwen = _model_dir(tmp_path, "Champ", QWEN_TEMPLATE)
        b = _Backend({"gemma-3-27b-it-4bit": str(gem), "Champ": str(qwen)})

        assert modelcaps.for_model(b, "gemma-3-27b-it-4bit").usable is False
        assert modelcaps.for_model(b, "Champ").usable is True
        # second call is cached (no re-read)
        assert modelcaps.for_model(b, "Champ").state == modelcaps.SUPPORTED

    def test_remote_endpoint_is_unknown_not_disarmed(self, tmp_path):
        b = _Backend({"Champ": "/on/the/other/host"},
                     base_url="http://m5.tailca7308.ts.net:8000")
        cap = modelcaps.for_model(b, "Champ")
        assert cap.state == modelcaps.UNKNOWN and cap.usable is True

    def test_a_broken_backend_is_unknown(self):
        class Boom:
            base_url = "http://127.0.0.1:8000"

            def model_paths(self):
                raise OSError(60, "Operation timed out")

        assert modelcaps.for_model(Boom(), "Champ").usable is True

    def test_no_model_id(self):
        assert modelcaps.for_model(_Backend({}), "").state == modelcaps.UNKNOWN


# --- turn wiring -------------------------------------------------------------


def test_a_no_tool_model_gets_an_empty_tool_surface(tmp_path, monkeypatch):
    """prepare_turn must hand run_single NO tools for such a model, and flag the
    session so the prompt says why."""
    from luxe.chat import repl
    from luxe.chat import slots as slots_mod
    from luxe.memory import session as session_store

    gem = _model_dir(tmp_path, "gemma-3-27b-it-4bit", GEMMA_TEMPLATE)

    class B(_Backend):
        def __init__(self, **k):
            super().__init__({"gemma-3-27b-it-4bit": str(gem)})

        def unload_all_loaded(self, *, except_for=None):
            return {}

        def thermal_guard(self, *a, **k):
            return True

    monkeypatch.setattr(slots_mod, "Backend", B)
    cfg = PipelineConfig(
        models={"monolith": "gemma-3-27b-it-4bit"},
        roles={"monolith": RoleConfig(model_key="monolith",
                                      tools=["read_file", "grep", "write_file"])},
    )
    sm = slots_mod.SlotManager(cfg)
    session = ChatSession(repo_path=str(tmp_path))
    session.session_id = session_store.new_session(repo_path=str(tmp_path)).session_id

    prep = repl.prepare_turn("hi", session, sm, cfg, frozenset(), lambda m: "review")

    assert prep.role_cfg.tools == []
    assert session.tools_withheld is True


def test_the_prompt_explains_withheld_tools():
    from luxe.agents.prompts import NO_TOOLS_MODEL_HINT

    s = ChatSession(write_enabled=True)
    s.tools_withheld = True
    ctx, _ = s.build_extra_context("summarise our chat")

    assert NO_TOOLS_MODEL_HINT in ctx
    assert "/model chat" in ctx                      # the way out
    assert "cannot call tools" in NO_TOOLS_MODEL_HINT
    # It must forbid pretending, which is the failure oMLX's silent drop invites.
    assert "Do NOT claim to have read" in NO_TOOLS_MODEL_HINT


# --- roster filter -----------------------------------------------------------


class TestVisibleModels:
    def _cfg(self, visible):
        return PipelineConfig(models={"monolith": "Champ"},
                              roles={"monolith": RoleConfig(model_key="monolith")},
                              visible_models=visible)

    def test_filters_to_the_roster_preserving_server_order(self):
        cfg = self._cfg(["b", "a"])
        assert cfg.visible(["a", "junk", "b"]) == ["a", "b"]

    def test_empty_roster_shows_everything(self):
        cfg = self._cfg([])
        assert cfg.visible(["a", "b"]) == ["a", "b"]

    def test_roster_entries_the_server_lacks_are_dropped(self):
        cfg = self._cfg(["a", "not-served"])
        assert cfg.visible(["a"]) == ["a"]

    def test_slot_manager_applies_it(self, monkeypatch):
        from luxe.chat import slots as slots_mod

        class B:
            base_url = ""
            api_key = ""

            def __init__(self, **k):
                pass

            def list_models(self):
                return ["Champ", "stale-bakeoff-entry",
                        "mlx-community--Champ", "gemma-3-27b-it-4bit"]

        monkeypatch.setattr(slots_mod, "Backend", B)
        sm = slots_mod.SlotManager(self._cfg(["Champ", "gemma-3-27b-it-4bit"]))

        assert sm.available_models() == ["Champ", "gemma-3-27b-it-4bit"]

    def test_the_shipped_config_roster_matches_the_working_set(self):
        """The fallback-kit roster (2026-07-30): exactly the union of the
        fleet manifests. gemma is OUT — no tool support (oMLX silently drops
        the tools array for it), and the kit has no seat for a
        conversation-only model."""
        from luxe.config import load_config

        cfg = load_config("configs/chat.yaml")
        assert set(cfg.visible_models) == {
            "Qwen3.6-35B-A3B-6bit",     # bench champion · m5 main
            "Qwen3.6-27B-6bit",         # m5 fallback
            "Qwen3.6-27B-4bit",         # m1/m4 main
            "Qwen3.6-35B-A3B-4bit",     # m1/m4 fallback
        }
        assert "gemma-3-27b-it-4bit" not in cfg.visible_models


# --- palette calm-down ------------------------------------------------------


def test_off_states_are_muted_not_red():
    """`write off` / `bash off` are safe defaults; red is for errors (the bar
    read as 'red, white, purple, blue' before this)."""
    from luxe.chat import slots as slots_mod
    from luxe.chat import theme as theme_mod
    from luxe.chat.status import StatusState, fields

    class B:
        base_url = ""
        api_key = ""

        def __init__(self, **k):
            pass

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(slots_mod, "Backend", B)
    try:
        cfg = PipelineConfig(models={"monolith": "C"},
                             roles={"monolith": RoleConfig(model_key="monolith")})
        sm = slots_mod.SlotManager(cfg)
        segs = fields(ChatSession(), sm, "", StatusState())
    finally:
        mp.undo()

    red = theme_mod.styles_for("error")[1]
    muted = theme_mod.styles_for("muted")[1]
    off_spans = [sp for seg in segs for sp in seg.spans if sp[0] == "off"]
    assert off_spans, "expected write/bash off chips"
    assert all(sp[2] == muted for sp in off_spans)
    assert not any(sp[2] == red for sp in off_spans)


def test_slot_chip_is_not_a_saturated_colour():
    from luxe.chat import theme as theme_mod

    theme_mod.set_palette("auto")
    try:
        ptk, rich = theme_mod.styles_for("slot")
        assert "blue" not in ptk and "blue" not in rich
        assert "magenta" not in ptk and "magenta" not in rich
    finally:
        theme_mod.set_palette(None)
        theme_mod.reset_cache()
