"""Tests for luxe.secrets — API-key resolution that doesn't depend on shell
hygiene (env → ~/.luxe/secrets.env → keychain). The 2026-07-30 m5 401."""

from __future__ import annotations

import luxe.secrets as secrets


def _no_keychain(monkeypatch):
    monkeypatch.setattr(secrets, "_from_keychain", lambda name: "")


def test_env_wins(monkeypatch, tmp_path):
    _no_keychain(monkeypatch)
    f = tmp_path / "secrets.env"
    f.write_text("OMLX_API_KEY=from-file\n")
    monkeypatch.setattr(secrets, "SECRETS_PATH", f)
    monkeypatch.setenv("OMLX_API_KEY", "from-env")
    assert secrets.resolve_api_key() == "from-env"


def test_file_fallback_parses_plain_export_and_quoted(monkeypatch, tmp_path):
    """secrets.env has NO export lines in the wild — a bare `source` leaves
    the env empty, which is exactly why luxe reads the file itself. All the
    common line shapes must parse."""
    _no_keychain(monkeypatch)
    f = tmp_path / "secrets.env"
    f.write_text(
        "# oMLX keys\n"
        "\n"
        "OMLX_API_KEY=omlx-plain\n"
        "export OMLX_API_KEY_M5=\"omlx-m5-quoted\"\n"
        "OTHER='single'\n"
    )
    monkeypatch.setattr(secrets, "SECRETS_PATH", f)
    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    monkeypatch.delenv("OMLX_API_KEY_M5", raising=False)
    assert secrets.resolve_api_key("OMLX_API_KEY") == "omlx-plain"
    assert secrets.resolve_api_key("OMLX_API_KEY_M5") == "omlx-m5-quoted"
    assert secrets.resolve_api_key("OTHER") == "single"


def test_missing_file_and_key_degrade_to_empty(monkeypatch, tmp_path):
    _no_keychain(monkeypatch)
    monkeypatch.setattr(secrets, "SECRETS_PATH", tmp_path / "absent.env")
    monkeypatch.delenv("NOPE_KEY", raising=False)
    assert secrets.resolve_api_key("NOPE_KEY") == ""


def test_keychain_is_last_resort(monkeypatch, tmp_path):
    monkeypatch.setattr(secrets, "SECRETS_PATH", tmp_path / "absent.env")
    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    monkeypatch.setattr(secrets, "_from_keychain",
                        lambda name: "kc-" + name)
    assert secrets.resolve_api_key() == "kc-OMLX_API_KEY"


def test_slot_manager_resolves_entry_key_via_secrets(monkeypatch, tmp_path):
    """SlotManager._build_backend must reach keys a shell never exported."""
    from luxe.chat import slots as slots_mod
    from luxe.config import BackendEntry, PipelineConfig, RoleConfig

    f = tmp_path / "secrets.env"
    f.write_text("OMLX_API_KEY_M5=tailnet-key\n")
    monkeypatch.setattr(secrets, "SECRETS_PATH", f)
    monkeypatch.delenv("OMLX_API_KEY_M5", raising=False)
    _no_keychain(monkeypatch)

    seen = {}

    class _B:
        def __init__(self, base_url="", model="", timeout_s=0.0, api_key=""):
            seen["api_key"] = api_key
            self.base_url, self.model = base_url, model

    monkeypatch.setattr(slots_mod, "Backend", _B)
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
        backends={"m5": BackendEntry(base_url="http://m5:8000",
                                     api_key_env="OMLX_API_KEY_M5",
                                     default=True)},
    )
    slots_mod.SlotManager(cfg)
    assert seen["api_key"] == "tailnet-key"
