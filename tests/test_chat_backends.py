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

    def __init__(self, base_url="", model="", timeout_s=600.0, api_key=""):
        self.base_url = base_url
        self.model = model
        self.timeout_s = timeout_s
        self.api_key = api_key
        self.unload_calls: list = []
        self.thermal_calls: list = []

    def health(self):
        return type(self).healthy.get(self.base_url, True)

    def list_models(self):
        return list(type(self).served.get(self.base_url, []))

    def unload_all_loaded(self, *, except_for=None):
        self.unload_calls.append(except_for)
        return {}

    def thermal_guard(self, target_model, **kw):
        self.thermal_calls.append(target_model)
        return True


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    FakeBackend.healthy = {}
    FakeBackend.served = {}
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
    sm = slots_mod.SlotManager(_multi_cfg())
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
