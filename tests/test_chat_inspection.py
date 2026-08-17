"""Tests for chat/inspection.py — the logic behind `/export`, `/diff`, and
`/doctor`. Real git repos in tmp_path; no model, no network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe.chat import inspection
from luxe.chat.session import ChatSession
from luxe.chat.slots import SlotManager
from luxe.config import PipelineConfig, RoleConfig
from luxe.memory import session as session_store


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def _repo(tmp_path: Path, *, commit: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.py").write_text("one\ntwo\nthree\n")
    if commit:
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


# --- /export ----------------------------------------------------------------


class TestExport:
    def _session(self) -> str:
        meta = session_store.new_session(repo_path="/x/repo", project_hash="h",
                                        slot_models={"chat": "Champ"},
                                        backend_name="local",
                                        base_url="http://127.0.0.1:8000")
        session_store.append_turn(meta.session_id, "user", text="why is it slow?",
                                  slot="chat")
        session_store.append_turn(meta.session_id, "assistant", text="Because of X.",
                                  run_id="r0", steps=3, tool_calls=5,
                                  backend="local")
        return meta.session_id

    def test_markdown_has_header_and_both_sides(self):
        sid = self._session()
        meta, records = session_store.load_session(sid)
        md = inspection.transcript_markdown(meta, records)

        assert md.startswith(f"# luxe chat — {sid}")
        assert "/x/repo" in md and "local" in md and "Champ" in md
        assert "**Turns:** 1" in md
        assert "### 1 · you" in md and "why is it slow?" in md
        assert "### 1 · luxe" in md and "Because of X." in md
        assert "steps 3" in md and "tools 5" in md

    def test_markdown_marks_interrupted_and_empty_replies(self):
        meta = session_store.new_session(repo_path="/x")
        session_store.append_turn(meta.session_id, "user", text="hi", slot="chat")
        session_store.append_turn(meta.session_id, "assistant", text="",
                                  interrupted=True)
        m, recs = session_store.load_session(meta.session_id)
        md = inspection.transcript_markdown(m, recs)
        assert "INTERRUPTED" in md and "_(no reply)_" in md

    def test_markdown_summarises_attachments_instead_of_inlining(self):
        meta = session_store.new_session(repo_path="/x")
        session_store.append_turn(meta.session_id, "attachment",
                                  paths=["big.py"], chars=48000)
        m, recs = session_store.load_session(meta.session_id)
        md = inspection.transcript_markdown(m, recs)
        assert "attached: `big.py`" in md
        assert "48000" not in md

    def test_markdown_renders_error_records(self):
        """A failed turn exports as part of the story, not a silent gap."""
        meta = session_store.new_session(repo_path="/x")
        session_store.append_turn(meta.session_id, "user", text="hi", slot="chat")
        session_store.append_turn(meta.session_id, "error",
                                  text="BackendError: 502", model="Main-M")
        m, recs = session_store.load_session(meta.session_id)
        md = inspection.transcript_markdown(m, recs)
        assert "⚠ turn failed (Main-M): BackendError: 502" in md

    def test_export_writes_beside_the_transcript_by_default(self):
        sid = self._session()
        out = inspection.export_transcript(sid)
        assert out == session_store.session_dir(sid) / "transcript.md"
        assert "why is it slow?" in out.read_text()

    def test_export_accepts_an_explicit_path(self, tmp_path):
        sid = self._session()
        dest = tmp_path / "out" / "chat.md"
        out = inspection.export_transcript(sid, dest)
        assert out == dest and dest.is_file()

    def test_export_into_a_directory_names_the_file(self, tmp_path):
        sid = self._session()
        d = tmp_path / "exports"
        d.mkdir()
        out = inspection.export_transcript(sid, d)
        assert out.parent == d and sid in out.name

    def test_export_of_an_unknown_session_raises(self):
        with pytest.raises(FileNotFoundError):
            inspection.export_transcript("nope")


# --- /diff ------------------------------------------------------------------


class TestSessionDiff:
    def test_reports_modifications_against_head(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "a.py").write_text("one\ntwo\nfour\nfive\n")

        diffs, err = inspection.session_diff(str(repo))

        assert err == ""
        assert [d.path for d in diffs] == ["a.py"]
        assert diffs[0].added == 2 and diffs[0].removed == 1

    def test_includes_staged_changes(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "b.py").write_text("new\n")
        subprocess.run(["git", "add", "b.py"], cwd=repo, check=True)

        diffs, _ = inspection.session_diff(str(repo))
        assert "b.py" in [d.path for d in diffs]

    def test_reports_untracked_files_as_new(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "scaffolded.py").write_text("x\n")

        diffs, _ = inspection.session_diff(str(repo))

        new = [d for d in diffs if d.path == "scaffolded.py"]
        assert new and new[0].untracked and new[0].is_empty is False

    def test_scopes_to_the_given_paths(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "a.py").write_text("changed\n")
        (repo / "other.py").write_text("also changed\n")

        diffs, _ = inspection.session_diff(str(repo), ["a.py"])
        assert [d.path for d in diffs] == ["a.py"]

    def test_clean_tree_is_empty_not_an_error(self, tmp_path):
        repo = _repo(tmp_path)
        diffs, err = inspection.session_diff(str(repo))
        assert diffs == [] and err == ""

    def test_repo_without_commits_still_works(self, tmp_path):
        repo = _repo(tmp_path, commit=False)   # no HEAD to diff against
        diffs, err = inspection.session_diff(str(repo))
        assert err == ""
        assert "a.py" in [d.path for d in diffs]     # surfaced as untracked

    def test_missing_repo_path_is_reported(self):
        diffs, err = inspection.session_diff("")
        assert diffs == [] and "no repo" in err

    def test_non_repo_directory_is_reported(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        diffs, err = inspection.session_diff(str(plain))
        assert diffs == [] and err

    def test_full_diff_returns_the_patch(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "a.py").write_text("one\ntwo\nfour\n")
        patch = inspection.full_diff(str(repo), ["a.py"])
        assert "-three" in patch and "+four" in patch


# --- /doctor ----------------------------------------------------------------


class _Backend:
    def __init__(self, *, healthy=True, key="k", models=("Champ",),
                 paths=None, base_url="http://127.0.0.1:8000"):
        self.base_url = base_url
        self.api_key = key
        self._healthy = healthy
        self._models = list(models)
        self._paths = paths or {}

    def health(self):
        return self._healthy

    def list_models(self):
        return list(self._models)

    def model_paths(self):
        return dict(self._paths)

    def unload_all_loaded(self, *, except_for=None):
        return {}

    def thermal_guard(self, *a, **k):
        return True


@pytest.fixture
def doctor_ctx(tmp_path, monkeypatch):
    from luxe import buildinfo
    from luxe.chat import origin as origin_mod
    from luxe.chat import slots as slots_mod

    origin_mod.reset_cache()
    monkeypatch.setattr(origin_mod, "network_mounts", lambda **k: [])
    # The update check must never hit the network in tests (nor slow them by
    # its 4s fetch timeout) — default to the offline branch.
    monkeypatch.setattr(buildinfo, "fetch_origin", lambda **k: False)
    cfg = PipelineConfig(models={"monolith": "Champ"},
                         roles={"monolith": RoleConfig(model_key="monolith")})
    monkeypatch.setattr(slots_mod, "Backend", lambda **k: _Backend())
    sm = SlotManager(cfg)
    repo = _repo(tmp_path)
    session = ChatSession(repo_path=str(repo))
    yield session, sm, str(repo)
    origin_mod.reset_cache()


def _states(doc) -> dict[str, str]:
    return {c.name: c.state for c in doc.checks}


class TestDoctor:
    def test_healthy_session_is_all_ok(self, doctor_ctx, tmp_path, monkeypatch):
        session, sm, repo = doctor_ctx
        sm.backend = _Backend(paths={"Champ": str(tmp_path / "models" / "Champ")})
        from luxe import search as search_mod
        monkeypatch.setattr(search_mod, "get_index",
                            lambda: type("I", (), {"paths": ["a.py"]})())

        doc = inspection.run_doctor(session, sm, repo)
        states = _states(doc)

        assert states["oMLX endpoint"] == inspection.OK
        assert states["chat model"] == inspection.OK
        assert states["weights"] == inspection.OK
        assert states["working tree"] == inspection.OK
        assert doc.worst in (inspection.OK, inspection.WARN)

    def test_dead_endpoint_fails_with_a_fix(self, doctor_ctx):
        session, sm, repo = doctor_ctx
        sm.backend = _Backend(healthy=False)

        doc = inspection.run_doctor(session, sm, repo)

        endpoint = next(c for c in doc.checks if c.name == "oMLX endpoint")
        assert endpoint.state == inspection.FAIL
        assert "omlx" in endpoint.fix.lower() or "/backend" in endpoint.fix
        assert doc.worst == inspection.FAIL

    def test_endpoint_exception_is_a_fail_not_a_crash(self, doctor_ctx):
        session, sm, repo = doctor_ctx

        class Boom(_Backend):
            def health(self):
                raise OSError(60, "Operation timed out")

        sm.backend = Boom()
        doc = inspection.run_doctor(session, sm, repo)
        assert _states(doc)["oMLX endpoint"] == inspection.FAIL

    def test_update_check_offline_is_quiet(self, doctor_ctx):
        session, sm, repo = doctor_ctx
        doc = inspection.run_doctor(session, sm, repo)
        update = next(c for c in doc.checks if c.name == "update")
        assert update.state == inspection.OK
        assert "unchecked" in update.detail

    def test_update_check_behind_warns_with_fix(self, doctor_ctx, monkeypatch):
        from luxe import buildinfo

        session, sm, repo = doctor_ctx
        monkeypatch.setattr(buildinfo, "fetch_origin", lambda **k: True)
        monkeypatch.setattr(buildinfo, "behind_origin", lambda *a, **k: 3)
        doc = inspection.run_doctor(session, sm, repo)
        update = next(c for c in doc.checks if c.name == "update")
        assert update.state == inspection.WARN
        assert "3 commit" in update.detail and "luxe update" in update.fix

    def test_update_check_current_is_ok(self, doctor_ctx, monkeypatch):
        from luxe import buildinfo

        session, sm, repo = doctor_ctx
        monkeypatch.setattr(buildinfo, "fetch_origin", lambda **k: True)
        monkeypatch.setattr(buildinfo, "behind_origin", lambda *a, **k: 0)
        doc = inspection.run_doctor(session, sm, repo)
        update = next(c for c in doc.checks if c.name == "update")
        assert update.state == inspection.OK
        assert "current" in update.detail

    def test_no_hosts_block_reports_single_model_default(self, doctor_ctx):
        session, sm, repo = doctor_ctx
        doc = inspection.run_doctor(session, sm, repo)
        manifest = next(c for c in doc.checks if c.name == "host manifest")
        assert manifest.state == inspection.OK
        assert "none configured" in manifest.detail

    def test_unmatched_hostname_warns_with_fix(self, doctor_ctx, monkeypatch):
        import luxe.config as config_mod
        from luxe.config import HostManifest

        session, sm, repo = doctor_ctx
        monkeypatch.setattr(config_mod, "short_hostname", lambda: "zeta")
        sm.cfg.hosts = {"m1": HostManifest(main="A", fallback="B")}
        doc = inspection.run_doctor(session, sm, repo)
        manifest = next(c for c in doc.checks if c.name == "host manifest")
        assert manifest.state == inspection.WARN
        assert "zeta" in manifest.detail and "hosts:" in manifest.fix

    def test_manifest_weights_checked_on_local_endpoint(self, doctor_ctx,
                                                        monkeypatch):
        import luxe.config as config_mod
        import luxe.modelstore as ms
        from luxe.chat import origin as origin_mod
        from luxe.config import HostManifest

        session, sm, repo = doctor_ctx
        monkeypatch.setattr(config_mod, "short_hostname", lambda: "here")
        sm.cfg.hosts = {"here": HostManifest(main="Main-M", fallback="Fb-M",
                                             keep=["Bench-M"])}
        sm.manifest = sm.cfg.host_manifest()
        monkeypatch.setattr(origin_mod, "endpoint_is_local", lambda url: True)
        states = {"Main-M": "dangling", "Fb-M": "ok", "Bench-M": "missing"}
        monkeypatch.setattr(ms, "model_state",
                            lambda mid, *a, **k: states.get(mid, "missing"))

        doc = inspection.run_doctor(session, sm, repo)
        by_name = {c.name: c for c in doc.checks}
        assert by_name["weights:Main-M"].state == inspection.FAIL
        assert "pull" in by_name["weights:Main-M"].fix
        assert by_name["weights:Fb-M"].state == inspection.OK
        assert by_name["weights:Bench-M"].state == inspection.WARN

    def test_degraded_session_warns(self, doctor_ctx, monkeypatch):
        import luxe.config as config_mod
        from luxe.config import HostManifest

        session, sm, repo = doctor_ctx
        monkeypatch.setattr(config_mod, "short_hostname", lambda: "here")
        sm.cfg.hosts = {"here": HostManifest(main="Main-M", fallback="Fb-M")}
        # The doctor reads the SlotManager's resolved manifest (the object
        # degrade actually consults), not a fresh cfg lookup — keep the
        # late-mutated cfg and the manager coherent the way __init__ would.
        sm.manifest = sm.cfg.host_manifest()
        sm.degraded_from, sm.degraded_to = "Main-M", "Fb-M"
        doc = inspection.run_doctor(session, sm, repo)
        degraded = next(c for c in doc.checks if c.name == "degraded")
        assert degraded.state == inspection.WARN
        assert "Fb-M" in degraded.detail and "Main-M" in degraded.fix

    def test_missing_model_fails_and_suggests_pull(self, doctor_ctx):
        session, sm, repo = doctor_ctx
        sm.backend = _Backend(models=["SomethingElse"])

        doc = inspection.run_doctor(session, sm, repo)

        model = next(c for c in doc.checks if c.name == "chat model")
        assert model.state == inspection.FAIL
        assert "luxe pull" in model.fix

    def test_missing_api_key_warns(self, doctor_ctx):
        session, sm, repo = doctor_ctx
        sm.backend = _Backend(key="")
        doc = inspection.run_doctor(session, sm, repo)
        assert _states(doc)["API key"] == inspection.WARN

    def test_network_weights_warn_about_the_first_turn(self, doctor_ctx, tmp_path,
                                                      monkeypatch):
        from luxe.chat import origin as origin_mod

        session, sm, repo = doctor_ctx
        mount = tmp_path / "nas"
        monkeypatch.setattr(origin_mod, "network_mounts",
                            lambda **k: [(str(mount), "smbfs", "//kappa/m")])
        sm.backend = _Backend(paths={"Champ": str(mount / "Champ")})

        doc = inspection.run_doctor(session, sm, repo)

        weights = next(c for c in doc.checks if c.name == "weights")
        assert weights.state == inspection.WARN
        assert "network" in weights.fix

    def test_remote_endpoint_warns(self, doctor_ctx):
        session, sm, repo = doctor_ctx
        sm.backend = _Backend(base_url="http://m5.tailca7308.ts.net:8000")
        doc = inspection.run_doctor(session, sm, repo)
        weights = next(c for c in doc.checks if c.name == "weights")
        assert weights.state == inspection.WARN
        assert "m5.tailca7308.ts.net" in weights.detail

    def test_stale_index_warns_with_the_two_heads(self, doctor_ctx, monkeypatch):
        from luxe import search as search_mod

        session, sm, repo = doctor_ctx
        monkeypatch.setattr(search_mod, "get_index",
                            lambda: type("I", (), {"paths": []})())
        session.index_head = "deadbee"

        doc = inspection.run_doctor(session, sm, repo)

        fresh = next(c for c in doc.checks if c.name == "index freshness")
        assert fresh.state == inspection.WARN
        assert "deadbee" in fresh.detail and "reindex" in fresh.fix

    def test_missing_index_warns(self, doctor_ctx, monkeypatch):
        from luxe import search as search_mod

        session, sm, repo = doctor_ctx
        monkeypatch.setattr(search_mod, "get_index", lambda: None)
        doc = inspection.run_doctor(session, sm, repo)
        assert _states(doc)["search index"] == inspection.WARN

    def test_non_git_repo_warns(self, doctor_ctx, tmp_path):
        session, sm, _ = doctor_ctx
        plain = tmp_path / "plain"
        plain.mkdir()
        session.repo_path = str(plain)
        doc = inspection.run_doctor(session, sm, str(plain))
        assert _states(doc)["git"] == inspection.WARN

    def test_low_disk_warns(self, doctor_ctx, monkeypatch):
        session, sm, repo = doctor_ctx
        monkeypatch.setattr(inspection.shutil, "disk_usage",
                            lambda p: type("U", (), {"free": 1024**3})())
        doc = inspection.run_doctor(session, sm, repo)
        disk = next(c for c in doc.checks if c.name == "disk")
        assert disk.state == inspection.WARN and "headroom" in disk.fix

    def test_read_only_mode_points_at_write(self, doctor_ctx):
        session, sm, repo = doctor_ctx
        doc = inspection.run_doctor(session, sm, repo)
        mode = next(c for c in doc.checks if c.name == "mode")
        assert "read-only" in mode.detail and "/write" in mode.fix

    def test_dirty_tree_is_reported_as_ok_with_a_count(self, doctor_ctx, tmp_path):
        session, sm, repo = doctor_ctx
        (Path(repo) / "a.py").write_text("changed\n")
        doc = inspection.run_doctor(session, sm, repo)
        tree = next(c for c in doc.checks if c.name == "working tree")
        assert tree.state == inspection.OK and "1 file(s) changed" in tree.detail


class TestDoctorOnALlamaServerEngine:
    """`backends: {engine: llama-server}` — neo (2026-08-13).

    The chat path is unchanged (every supported engine is OpenAI-compatible);
    what these pin is that the oMLX-ONLY diagnostics stop lying. Each one is a
    line that used to be permanently yellow, or a `fix` naming a command the
    host does not have.
    """

    @pytest.fixture
    def llama_ctx(self, tmp_path, monkeypatch):
        from luxe import buildinfo
        from luxe.chat import origin as origin_mod
        from luxe.chat import slots as slots_mod
        from luxe.config import BackendEntry

        origin_mod.reset_cache()
        monkeypatch.setattr(origin_mod, "network_mounts", lambda **k: [])
        monkeypatch.setattr(buildinfo, "fetch_origin", lambda **k: False)
        cfg = PipelineConfig(
            models={"monolith": "Champ"},
            roles={"monolith": RoleConfig(model_key="monolith")},
            backends={"local": BackendEntry(base_url="http://127.0.0.1:8080",
                                            engine="llama-server",
                                            default=True)},
        )
        # llama-server has no /v1/models/status: no key, no model paths.
        made = _Backend(key="", paths={}, base_url="http://127.0.0.1:8080")
        monkeypatch.setattr(slots_mod, "Backend", lambda **k: made)
        sm = SlotManager(cfg)
        sm.backend = made
        repo = _repo(tmp_path)
        session = ChatSession(repo_path=str(repo))
        yield session, sm, str(repo)
        origin_mod.reset_cache()

    def test_endpoint_check_is_named_for_the_engine(self, llama_ctx):
        session, sm, repo = llama_ctx
        doc = inspection.run_doctor(session, sm, repo)
        names = {c.name for c in doc.checks}
        assert "llama-server endpoint" in names
        assert "oMLX endpoint" not in names

    def test_dead_endpoint_fix_does_not_mention_brew(self, llama_ctx):
        session, sm, repo = llama_ctx
        sm.backend = _Backend(healthy=False, key="",
                              base_url="http://127.0.0.1:8080")
        doc = inspection.run_doctor(session, sm, repo)
        check = next(c for c in doc.checks if c.name == "llama-server endpoint")
        assert check.state == inspection.FAIL
        assert "brew" not in check.fix
        assert "launchctl" in check.fix

    def test_missing_api_key_is_not_a_warning(self, llama_ctx):
        session, sm, repo = llama_ctx
        doc = inspection.run_doctor(session, sm, repo)
        key = next(c for c in doc.checks if c.name == "API key")
        assert key.state == inspection.OK
        assert "not required" in key.detail
        assert key.fix == ""

    def test_unreported_weight_path_is_not_a_warning(self, llama_ctx):
        session, sm, repo = llama_ctx
        doc = inspection.run_doctor(session, sm, repo)
        weights = next(c for c in doc.checks if c.name == "weights")
        assert weights.state == inspection.OK
        assert "no model path" in weights.detail

    def test_stale_omlx_build_check_is_skipped(self, llama_ctx, monkeypatch):
        session, sm, repo = llama_ctx
        called = []
        import luxe.staleproc as staleproc
        monkeypatch.setattr(staleproc, "check_omlx",
                            lambda: called.append(1) or None)
        doc = inspection.run_doctor(session, sm, repo)
        assert called == []
        assert not [c for c in doc.checks if c.name == "oMLX build"]

    def test_an_omlx_endpoint_keeps_every_old_line(self, doctor_ctx):
        """The control: nothing above may leak onto the default engine."""
        session, sm, repo = doctor_ctx
        sm.backend = _Backend(key="")
        doc = inspection.run_doctor(session, sm, repo)
        states = _states(doc)
        assert "oMLX endpoint" in states
        key = next(c for c in doc.checks if c.name == "API key")
        assert key.state == inspection.WARN and "secrets.env" in key.fix
        assert _states(doc)["weights"] == inspection.WARN


class TestDoctorOnAnOpenRouterEngine:
    """`backends: {engine: openrouter}` — the cloud carve-out (2026-08-17).

    Three lines have to change, and each one used to say something actively
    wrong on a metered, provider-hosted endpoint: a fix that says "start the
    server" (there is no local process), a missing key reported as a caveat
    (it is the whole session — every request 401s), and a weights WARN that
    tells you to `luxe pull` bytes that will never live on this disk.
    """

    @pytest.fixture
    def cloud_ctx(self, tmp_path, monkeypatch):
        from luxe import buildinfo
        from luxe.chat import origin as origin_mod
        from luxe.chat import slots as slots_mod
        from luxe.config import BackendEntry

        origin_mod.reset_cache()
        monkeypatch.setattr(origin_mod, "network_mounts", lambda **k: [])
        monkeypatch.setattr(buildinfo, "fetch_origin", lambda **k: False)
        cfg = PipelineConfig(
            models={"monolith": "org/cloud-model"},
            roles={"monolith": RoleConfig(model_key="monolith")},
            backends={"openrouter": BackendEntry(
                base_url="https://openrouter.ai/api", engine="openrouter",
                api_key_env="OPENROUTER_API_KEY", budget_usd=5.0,
                default=True)},
        )
        made = _Backend(models=("org/cloud-model",), paths={},
                        base_url="https://openrouter.ai/api")
        monkeypatch.setattr(slots_mod, "Backend", lambda **k: made)
        sm = SlotManager(cfg)
        sm.backend = made
        repo = _repo(tmp_path)
        session = ChatSession(repo_path=str(repo))
        yield session, sm, str(repo)
        origin_mod.reset_cache()

    def test_the_endpoint_check_is_named_for_the_provider(self, cloud_ctx):
        session, sm, repo = cloud_ctx
        doc = inspection.run_doctor(session, sm, repo)
        names = {c.name for c in doc.checks}
        assert "OpenRouter endpoint" in names
        assert "oMLX endpoint" not in names

    def test_a_dead_endpoint_never_says_start_the_server(self, cloud_ctx):
        session, sm, repo = cloud_ctx
        sm.backend = _Backend(healthy=False,
                              base_url="https://openrouter.ai/api")
        doc = inspection.run_doctor(session, sm, repo)
        check = next(c for c in doc.checks if c.name == "OpenRouter endpoint")
        assert check.state == inspection.FAIL
        assert "brew" not in check.fix and "launchctl" not in check.fix
        assert "OPENROUTER_API_KEY" in check.fix

    def test_a_missing_key_is_a_FAIL_not_a_warning(self, cloud_ctx):
        session, sm, repo = cloud_ctx
        sm.backend = _Backend(key="", models=("org/cloud-model",),
                              base_url="https://openrouter.ai/api")
        doc = inspection.run_doctor(session, sm, repo)
        key = next(c for c in doc.checks if c.name == "API key")
        assert key.state == inspection.FAIL
        assert doc.worst == inspection.FAIL

    def test_the_key_check_names_the_env_var_and_never_a_value(self, cloud_ctx):
        """chat.sdd Must-not: names and presence only. A doctor table is
        written to a transcript and read by a model."""
        session, sm, repo = cloud_ctx
        sm.backend = _Backend(key="", models=("org/cloud-model",),
                              base_url="https://openrouter.ai/api")
        doc = inspection.run_doctor(session, sm, repo)
        key = next(c for c in doc.checks if c.name == "API key")
        assert "OPENROUTER_API_KEY" in key.detail
        assert "OPENROUTER_API_KEY" in key.fix
        assert "secrets.env" in key.fix
        assert "sk-" not in key.fix and "sk-" not in key.detail

    def test_weights_are_provider_hosted_and_say_they_are_billable(self, cloud_ctx):
        session, sm, repo = cloud_ctx
        doc = inspection.run_doctor(session, sm, repo)
        weights = next(c for c in doc.checks if c.name == "weights")
        assert weights.state == inspection.OK
        assert "provider-hosted" in weights.detail
        assert "billable" in weights.detail
        assert "luxe pull" not in weights.fix

    def test_the_stale_omlx_build_check_is_skipped(self, cloud_ctx, monkeypatch):
        session, sm, repo = cloud_ctx
        called = []
        import luxe.staleproc as staleproc
        monkeypatch.setattr(staleproc, "check_omlx",
                            lambda: called.append(1) or None)
        doc = inspection.run_doctor(session, sm, repo)
        assert called == []
        assert not [c for c in doc.checks if c.name == "oMLX build"]
