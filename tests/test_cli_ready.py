"""`luxe ready` + `luxe outage` — the anti-fumble layer.

`ready` is the host-level twin of `/doctor`: same checks, same renderer, a
verdict and an exit code. `outage` is the offline card. Neither may need a
model, and neither may go near the network beyond doctor's one ≤4s fetch.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from luxe import cli
from luxe import outage as outage_mod
from luxe.chat import inspection
from luxe.chat import slots as slots_mod
from luxe.config import PipelineConfig, RoleConfig


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """The update check is the only networked doctor line — pin it offline so
    the suite never waits on (or depends on) a fetch."""
    from luxe import buildinfo
    monkeypatch.setattr(buildinfo, "fetch_origin", lambda **k: False)
    from luxe.chat import origin as origin_mod
    origin_mod.reset_cache()
    monkeypatch.setattr(origin_mod, "network_mounts", lambda **k: [])
    yield
    origin_mod.reset_cache()


class _Backend:
    """A healthy oMLX stand-in, or a dead one when `healthy=False`."""

    healthy = True
    models = ["Champ"]

    def __init__(self, base_url="", model="", timeout_s=600.0, api_key="k", **kw):
        self.base_url = base_url or "http://127.0.0.1:8000"
        self.model = model
        self.api_key = api_key

    def health(self):
        return self.healthy

    def list_models(self):
        if not self.healthy:
            raise OSError("connection refused")
        return list(self.models)

    def model_paths(self):
        return {}

    def unload_all_loaded(self, *, except_for=None):
        return {}

    def thermal_guard(self, *a, **k):
        return True


def _cfg() -> PipelineConfig:
    return PipelineConfig(models={"monolith": "Champ"},
                          roles={"monolith": RoleConfig(model_key="monolith")})


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _use_backend(monkeypatch, *, healthy=True, models=("Champ",)):
    class B(_Backend):
        pass
    B.healthy = healthy
    B.models = list(models)
    monkeypatch.setattr(slots_mod, "Backend", B)
    return B


class TestReadyDoctor:
    def test_healthy_host_is_all_ok(self, tmp_path, monkeypatch):
        _use_backend(monkeypatch)
        doc = cli.build_ready_doctor(_cfg(), str(_repo(tmp_path)))
        assert doc.worst != inspection.FAIL
        names = {c.name for c in doc.checks}
        assert {"oMLX endpoint", "API key", "chat model", "disk"} <= names

    def test_dead_endpoint_fails_with_runnable_fixes(self, tmp_path, monkeypatch):
        _use_backend(monkeypatch, healthy=False)
        doc = cli.build_ready_doctor(_cfg(), str(_repo(tmp_path)))
        assert doc.worst == inspection.FAIL
        endpoint = next(c for c in doc.checks if c.name == "oMLX endpoint")
        assert endpoint.state == inspection.FAIL
        assert "omlx" in endpoint.fix.lower() or "/backend" in endpoint.fix

    def test_missing_model_in_catalog_fails(self, tmp_path, monkeypatch):
        _use_backend(monkeypatch, models=("SomethingElse",))
        doc = cli.build_ready_doctor(_cfg(), str(_repo(tmp_path)))
        model = next(c for c in doc.checks if c.name == "chat model")
        assert model.state == inspection.FAIL
        assert "luxe pull" in model.fix

    def test_no_api_key_warns(self, tmp_path, monkeypatch):
        B = _use_backend(monkeypatch)
        monkeypatch.setattr(B, "__init__",
                            lambda self, **kw: _Backend.__init__(self, api_key=""))
        doc = cli.build_ready_doctor(_cfg(), str(_repo(tmp_path)))
        key = next(c for c in doc.checks if c.name == "API key")
        assert key.state == inspection.WARN and "OMLX_API_KEY" in key.fix

    def test_session_scoped_lines_are_restated_not_claimed(self, tmp_path,
                                                           monkeypatch):
        """`ready` builds a stand-in session; its read-only/no-index state must
        not be reported as if it described the user's next chat session."""
        _use_backend(monkeypatch)
        doc = cli.build_ready_doctor(_cfg(), str(_repo(tmp_path)))
        by = {c.name: c for c in doc.checks}
        assert "n/a outside a session" in by["mode"].detail
        assert by["mode"].state == inspection.OK and not by["mode"].fix
        assert by["search index"].state == inspection.OK
        assert not by["search index"].fix

    def test_every_warn_or_fail_carries_a_fix(self, tmp_path, monkeypatch):
        """The whole point of the table under pressure: no dead ends."""
        _use_backend(monkeypatch, healthy=False)
        doc = cli.build_ready_doctor(_cfg(), str(_repo(tmp_path)))
        missing = [c.name for c in doc.checks
                   if c.state != inspection.OK and not c.fix]
        assert missing == [], f"WARN/FAIL lines with no fix: {missing}"


class TestRenderParity:
    def test_same_doctor_renders_identically_for_slash_doctor_and_ready(self):
        """One renderer, two callers — `/doctor` and `luxe ready` must print
        the same bytes for the same Doctor or the card rots."""
        doc = inspection.Doctor()
        doc.add("oMLX endpoint", inspection.OK, "local http://x")
        doc.add("chat model", inspection.FAIL, "nope", "`luxe pull Champ`")

        out_a, out_b = io.StringIO(), io.StringIO()
        inspection.render_doctor(doc, Console(file=out_a, force_terminal=False,
                                              width=100))
        inspection.render_doctor(doc, Console(file=out_b, force_terminal=False,
                                              width=100), title="luxe ready")
        a = out_a.getvalue().splitlines()
        b = out_b.getvalue().splitlines()
        assert a[0].strip() == "Doctor" and b[0].strip() == "luxe ready"
        assert a[1:] == b[1:]

    def test_fix_lines_only_render_for_non_ok(self):
        doc = inspection.Doctor()
        doc.add("fine", inspection.OK, "yes", "`should not print`")
        out = io.StringIO()
        inspection.render_doctor(doc, Console(file=out, force_terminal=False,
                                              width=100))
        assert "should not print" not in out.getvalue()


class TestReadyCommand:
    def test_exit_zero_when_healthy(self, tmp_path, monkeypatch):
        _use_backend(monkeypatch)
        monkeypatch.setattr(cli, "_default_chat_config",
                            lambda: str(_write_cfg(tmp_path)))
        res = CliRunner().invoke(cli.main, ["ready", "--repo",
                                            str(_repo(tmp_path))])
        assert res.exit_code == 0, res.output
        assert "READY" in res.output
        assert "luxe smoke" in res.output

    def test_exit_one_and_points_at_the_card_when_broken(self, tmp_path,
                                                         monkeypatch):
        _use_backend(monkeypatch, healthy=False)
        monkeypatch.setattr(cli, "_default_chat_config",
                            lambda: str(_write_cfg(tmp_path)))
        res = CliRunner().invoke(cli.main, ["ready", "--repo",
                                            str(_repo(tmp_path))])
        assert res.exit_code == 1, res.output
        assert "NOT READY" in res.output
        assert "luxe outage" in res.output

    def test_unknown_backend_exits_two(self, tmp_path, monkeypatch):
        _use_backend(monkeypatch)
        monkeypatch.setattr(cli, "_default_chat_config",
                            lambda: str(_write_cfg(tmp_path)))
        res = CliRunner().invoke(cli.main, ["ready", "--backend", "nope"])
        assert res.exit_code == 2 and "Unknown backend" in res.output

    def test_doctor_is_an_alias_for_ready(self):
        assert cli.main._aliases.get("doctor") == "ready"


def _write_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "chat.yaml"
    p.write_text(
        "omlx_base_url: http://127.0.0.1:8000\n"
        "models:\n  monolith: Champ\n"
        "roles:\n  monolith:\n    model_key: monolith\n")
    return p


class TestOutageCard:
    def test_command_prints_the_card(self):
        res = CliRunner().invoke(cli.main, ["outage", "--plain"])
        assert res.exit_code == 0
        for sentinel in ("luxe ready", "luxe smoke", "/write", "debug.log"):
            assert sentinel in res.output, sentinel

    def test_card_exists_and_is_short(self):
        text = outage_mod.CARD_PATH.read_text()
        assert len(text.splitlines()) <= 120

    def test_every_luxe_command_named_by_the_card_exists(self):
        """The anti-rot test: the card is only useful if its commands are real."""
        known = set(cli.main.commands) | set(cli.main._aliases)
        named = outage_mod.referenced_commands()
        assert named, "the card names no luxe subcommands — parser broken?"
        missing = sorted(named - known)
        assert missing == [], f"OUTAGE.md names non-existent commands: {missing}"

    def test_load_card_never_raises_on_a_missing_file(self, monkeypatch):
        monkeypatch.setattr(outage_mod, "CARD_PATH", Path("/nope/OUTAGE.md"))
        text = outage_mod.load_card()
        assert "luxe ready" in text


class TestDrillRuleManifestResolution:
    """`luxe ready --backend <remote>` judges the ENDPOINT host's manifest.

    The chat.sdd drill rule (2026-08-12): before this, the stand-in doctor
    resolved models from the LOCAL host's manifest, so from m1 a
    `ready --backend m5` judged m1's 4-bit main against m5's catalog and was
    structurally NOT READY even with endpoint and key green.
    """

    @staticmethod
    def _fleet_cfg() -> PipelineConfig:
        from luxe.config import BackendEntry, HostManifest
        return PipelineConfig(
            models={"monolith": "Champ"},
            roles={"monolith": RoleConfig(model_key="monolith")},
            hosts={"here": HostManifest(main="Local-M", fallback="Local-Fb"),
                   "m5": HostManifest(main="Remote-M", fallback="Remote-Fb")},
            backends={
                "local": BackendEntry(base_url="http://127.0.0.1:8000"),
                "m5": BackendEntry(base_url="http://m5.tailexample.ts.net:8000",
                                   default=True),
            },
        )

    def test_host_for_endpoint_local_resolves_to_this_host(self, monkeypatch):
        from luxe.chat import origin
        monkeypatch.setattr("luxe.config.short_hostname", lambda: "here")
        assert origin.host_for_endpoint("http://127.0.0.1:8000") == "here"

    def test_host_for_endpoint_remote_resolves_to_url_short_host(self):
        from luxe.chat.origin import host_for_endpoint
        assert host_for_endpoint("http://m5.tailexample.ts.net:8000") == "m5"

    def test_model_for_slot_manifest_host_selects_that_manifest(self, monkeypatch):
        monkeypatch.setattr("luxe.config.short_hostname", lambda: "here")
        cfg = self._fleet_cfg()
        assert cfg.model_for_slot("chat") == "Local-M"
        assert cfg.model_for_slot("chat", manifest_host="m5") == "Remote-M"

    def test_slotmanager_manifest_host_governs_manifest_and_models(
            self, monkeypatch):
        monkeypatch.setattr("luxe.config.short_hostname", lambda: "here")
        _use_backend(monkeypatch, models=("Remote-M", "Remote-Fb"))
        cfg = self._fleet_cfg()
        sm = slots_mod.SlotManager(cfg, manifest_host="m5")
        assert sm.manifest is not None and sm.manifest.main == "Remote-M"
        assert sm.model_for("chat") == "Remote-M"
        default_sm = slots_mod.SlotManager(cfg)
        assert default_sm.manifest is not None
        assert default_sm.manifest.main == "Local-M"
        assert default_sm.model_for("chat") == "Local-M"

    def test_ready_against_remote_backend_judges_remote_pair(
            self, tmp_path, monkeypatch):
        """The founding case: remote main IS served -> no chat-model FAIL."""
        monkeypatch.setattr("luxe.config.short_hostname", lambda: "here")
        _use_backend(monkeypatch, models=("Remote-M", "Remote-Fb"))
        doc = cli.build_ready_doctor(self._fleet_cfg(), str(_repo(tmp_path)))
        chat_model = next(c for c in doc.checks if c.name == "chat model")
        assert chat_model.state != inspection.FAIL
        assert "Remote-M" in chat_model.detail
        assert not any("Local-M" in (c.detail or "") for c in doc.checks
                       if c.state == inspection.FAIL)

    def test_ready_against_local_backend_is_byte_identical_to_session_rule(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr("luxe.config.short_hostname", lambda: "here")
        _use_backend(monkeypatch, models=("Local-M", "Local-Fb"))
        cfg = self._fleet_cfg()
        cfg.backends["m5"].default = False
        cfg.backends["local"].default = True
        doc = cli.build_ready_doctor(cfg, str(_repo(tmp_path)))
        chat_model = next(c for c in doc.checks if c.name == "chat model")
        assert "Local-M" in chat_model.detail
