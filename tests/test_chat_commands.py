"""Tests for chat slash-command dispatch."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from luxe.chat import commands as cmd
from luxe.chat import slots as slots_mod
from luxe.chat.session import ChatSession
from luxe.config import PipelineConfig, RoleConfig
from luxe.memory import project as project_mem


class FakeBackend:
    def __init__(self, base_url="", model="", timeout_s=600.0, api_key=""):
        self.base_url = base_url
        self.model = model
        self.timeout_s = timeout_s
        self.api_key = api_key

    def unload_all_loaded(self, *, except_for=None):
        return {}

    def thermal_guard(self, target_model, **kw):
        return True

    def health(self):
        return True

    def list_models(self):
        return []


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    monkeypatch.setattr(slots_mod, "Backend", FakeBackend)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def ctx(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
    )
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=100)
    session = ChatSession(repo_path=str(repo))
    sm = slots_mod.SlotManager(cfg)
    c = cmd.CommandContext(console=console, session=session, slots=sm)
    c._out = out  # type: ignore[attr-defined]
    c._repo = str(repo)  # type: ignore[attr-defined]
    return c


def _text(ctx) -> str:
    return ctx._out.getvalue()


def test_is_command():
    assert cmd.is_command("/help")
    assert not cmd.is_command("hello")


def test_help(ctx):
    res = cmd.dispatch("/help", ctx)
    assert res.handled and not res.exit
    assert "/model" in _text(ctx)


def test_quit_exits(ctx):
    assert cmd.dispatch("/quit", ctx).exit
    assert cmd.dispatch("/exit", ctx).exit


def test_unknown_command(ctx):
    cmd.dispatch("/frobnicate", ctx)
    assert "Unknown command" in _text(ctx)


def test_write_toggles(ctx):
    assert ctx.session.write_enabled is False
    cmd.dispatch("/write", ctx)
    assert ctx.session.write_enabled is True
    cmd.dispatch("/write", ctx)
    assert ctx.session.write_enabled is False


def test_use_pins_slot(ctx):
    cmd.dispatch("/use code", ctx)
    assert ctx.session.pinned_slot == "code"
    cmd.dispatch("/use bogus", ctx)
    assert "Usage" in _text(ctx)


def test_model_list_and_override(ctx):
    cmd.dispatch("/model", ctx)
    assert "chat" in _text(ctx)
    ctx.slots.cfg.models["coder"] = "Coder-Model"
    cmd.dispatch("/model code Coder-Model", ctx)
    assert ctx.slots.model_for("code") == "Coder-Model"


def test_model_numbered_picker(ctx, monkeypatch):
    monkeypatch.setattr(ctx.slots, "available_models", lambda: ["M-a", "M-b", "M-c"])
    cmd.dispatch("/model", ctx)
    out = _text(ctx)
    assert "available models" in out and "M-a" in out
    cmd.dispatch("/model chat 2", ctx)            # pick the 2nd available model
    assert ctx.slots.model_for("chat") == "M-b"


def test_model_picker_out_of_range(ctx, monkeypatch):
    monkeypatch.setattr(ctx.slots, "available_models", lambda: ["M-a"])
    cmd.dispatch("/model chat 9", ctx)
    assert "1" in _text(ctx) and ctx.slots.model_for("chat") != "M-a"


def test_model_omlx_unreachable_hint(ctx, monkeypatch):
    monkeypatch.setattr(ctx.slots, "available_models", lambda: [])
    cmd.dispatch("/model", ctx)
    assert "oMLX unreachable" in _text(ctx)


def test_clear_resets_turns(ctx):
    from luxe.chat.session import ChatTurn
    ctx.session.add_turn(ChatTurn(user="hi", assistant="yo"))
    cmd.dispatch("/clear", ctx)
    assert ctx.session.turns == []


def test_memory_add_list_promote_forget(ctx):
    repo = ctx.session.repo_path
    cmd.dispatch("/memory add this repo uses uv", ctx)
    mem = project_mem.load_memory(repo)
    assert len(mem.facts) == 1
    fid = mem.facts[0].id
    assert mem.facts[0].confidence == "manual"  # user-added → injected

    cmd.dispatch("/memory list", ctx)
    assert fid in _text(ctx)

    cmd.dispatch(f"/memory forget {fid}", ctx)
    assert project_mem.load_memory(repo).facts == []


def _ctx_with_ceiling(tmp_path, monkeypatch, num_ctx_max):
    monkeypatch.setattr(slots_mod, "Backend", FakeBackend)
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(
            model_key="monolith", num_ctx=32768, num_ctx_max=num_ctx_max)},
    )
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=120)
    c = cmd.CommandContext(console=console, session=ChatSession(),
                           slots=slots_mod.SlotManager(cfg))
    c._out = out  # type: ignore[attr-defined]
    return c


def test_ctx_show_lists_tiers_and_current(ctx):
    cmd.dispatch("/ctx", ctx)
    out = _text(ctx)
    assert "context window" in out
    assert "small" in out and "xlarge" in out


def test_ctx_set_within_ceiling_no_clamp(tmp_path, monkeypatch):
    c = _ctx_with_ceiling(tmp_path, monkeypatch, num_ctx_max=131072)
    cmd.dispatch("/ctx large", c)
    assert c.session.num_ctx_override == 65536
    assert "clamped" not in c._out.getvalue()


def test_ctx_set_above_ceiling_warns_and_clamps(tmp_path, monkeypatch):
    c = _ctx_with_ceiling(tmp_path, monkeypatch, num_ctx_max=8192)
    cmd.dispatch("/ctx xlarge", c)
    # Stored unclamped; the per-turn apply (repl) clamps to the ceiling.
    assert c.session.num_ctx_override == 131072
    out = c._out.getvalue()
    assert "clamped to 8192" in out


def test_ctx_unknown_tier(ctx):
    cmd.dispatch("/ctx humongous", ctx)
    assert "Unknown size" in _text(ctx)


def test_bash_mode_toggles(ctx):
    assert ctx.session.unrestricted_bash is False
    cmd.dispatch("/bash", ctx)
    assert ctx.session.unrestricted_bash is True
    assert "UNRESTRICTED" in _text(ctx)
    cmd.dispatch("/bash", ctx)
    assert ctx.session.unrestricted_bash is False
    assert "allowlisted" in _text(ctx)


def test_bash_mode_warns_when_read_only(ctx):
    # bash is only exposed in write mode; enabling unrestricted while read-only
    # should hint the user to /write.
    assert ctx.session.write_enabled is False
    cmd.dispatch("/bash", ctx)
    assert "/write" in _text(ctx)


def test_compare_hook_invoked(ctx):
    seen = []
    ctx.on_compare = lambda task: seen.append(task)
    cmd.dispatch("/compare fix the bug", ctx)
    assert seen == ["fix the bug"]


def test_compare_review_hook_invoked(ctx):
    seen = []
    ctx.on_compare_review = lambda cid: seen.append(cid)
    cmd.dispatch("/compare review abc123", ctx)
    assert seen == ["abc123"]


def test_resume_hook_invoked(ctx):
    seen = []
    ctx.on_resume = lambda sid: seen.append(sid)
    cmd.dispatch("/resume xyz", ctx)
    assert seen == ["xyz"]


@pytest.mark.parametrize("alias,kind", [
    ("/gitaudit", "gitaudit"),
    ("/git-audit", "gitaudit"),
    ("/gaudit", "gitaudit"),
    ("/gitchange", "gitchange"),
    ("/git-change", "gitchange"),
    ("/gchange", "gitchange"),
    # deprecated back-compat aliases → the two merged commands
    ("/gitsummary", "gitaudit"),
    ("/gitreview", "gitaudit"),
    ("/grev", "gitaudit"),
    ("/gitrefactor", "gitaudit"),
    ("/gitplan", "gitchange"),
    ("/gplan", "gitchange"),
])
def test_git_analysis_aliases_dispatch(ctx, alias, kind):
    seen = []
    ctx.on_git_analysis = lambda k, deep=None: seen.append((k, deep))
    res = cmd.dispatch(alias, ctx)
    assert res.handled and not res.exit
    assert seen == [(kind, None)]   # no arg → auto (deep=None)


@pytest.mark.parametrize("arg,expected", [
    ("deep", True), ("shallow", False), ("no-deep", False), ("", None),
])
def test_git_analysis_deep_arg(ctx, arg, expected):
    seen = []
    ctx.on_git_analysis = lambda k, deep=None: seen.append((k, deep))
    cmd.dispatch(f"/gitaudit {arg}".strip(), ctx)
    assert seen == [("gitaudit", expected)]


def test_compact_toggles_session_flag(ctx):
    assert ctx.session.compact is False
    res = cmd.dispatch("/compact", ctx)
    assert res.handled and ctx.session.compact is True
    assert "compact" in _text(ctx).lower()
    cmd.dispatch("/compact", ctx)
    assert ctx.session.compact is False


def test_compact_listed_in_help(ctx):
    cmd.dispatch("/help", ctx)
    assert "/compact" in _text(ctx)


def test_git_analysis_no_repo_points_at_cli(ctx):
    ctx.session.repo_path = ""
    seen = []
    ctx.on_git_analysis = lambda k, deep=None: seen.append(k)
    cmd.dispatch("/gitaudit", ctx)
    assert seen == []  # hook NOT called when no repo is bound
    out = _text(ctx)
    assert "luxe gitaudit" in out


# --- /model provenance markers ----------------------------------------------


def test_model_list_flags_where_each_model_lives(ctx, tmp_path, monkeypatch):
    """`/model` must say which models are on this disk and which come over the
    network — the distinction was invisible before 2026-07-29."""
    from luxe.chat import origin as origin_mod

    origin_mod.reset_cache()
    mount = tmp_path / "Volumes" / "nas"
    monkeypatch.setattr(origin_mod, "network_mounts",
                        lambda **k: [(str(mount), "smbfs", "//kappa/models")])
    monkeypatch.setattr(ctx.slots.backend, "model_paths",
                        lambda: {"Champ": str(tmp_path / "local" / "Champ"),
                                 "Faraway": str(mount / "Faraway")},
                        raising=False)
    monkeypatch.setattr(ctx.slots, "available_models",
                        lambda: ["Champ", "Faraway"])

    cmd.dispatch("/model", ctx)
    out = _text(ctx)

    assert "⌂ local" in out          # on this disk
    assert "☁ network" in out        # streams over SMB
    assert "⌂ local disk" in out     # legend
    origin_mod.reset_cache()


# --- /pull ------------------------------------------------------------------


class _FakeAdmin:
    """Stands in for OmlxAdmin: records calls, never touches the network."""

    started: list = []

    def __init__(self, *a, **k):
        self.base_url = k.get("base_url", "")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def tasks(self):
        return []

    def search(self, query, **k):
        from luxe.modelstore import HFModel
        return [HFModel(repo_id=f"mlx-community/{query}-4bit", downloads=7,
                        size_bytes=1024)]

    def model_info(self, repo_id):
        return {"size": 2048}

    def start_download(self, repo_id, hf_token=""):
        from luxe.modelstore import DownloadTask
        type(self).started.append(repo_id)
        return DownloadTask(task_id="t1", repo_id=repo_id, status="pending",
                            total_size=2048)

    def wait_for(self, task_id, **k):
        from luxe.modelstore import DownloadTask
        return DownloadTask(task_id=task_id, repo_id="org/Champ",
                            status="completed", progress=100,
                            total_size=2048, downloaded_size=2048)


@pytest.fixture
def _no_network_pull(monkeypatch):
    from luxe import modelstore as ms

    _FakeAdmin.started = []
    monkeypatch.setattr(ms, "OmlxAdmin", _FakeAdmin)
    monkeypatch.setattr(ms, "network_mounts", lambda: [])
    return _FakeAdmin


def test_pull_bare_lists_local_models(ctx, _no_network_pull, monkeypatch):
    from luxe import modelstore as ms

    monkeypatch.setattr(ms, "local_model_names", lambda *a, **k: ["Champ"])
    cmd.dispatch("/pull", ctx)
    out = _text(ctx)
    assert "Local models" in out and "Champ" in out


def test_pull_search_lists_hf_hits(ctx, _no_network_pull):
    cmd.dispatch("/pull --search Qwen", ctx)
    assert "mlx-community/Qwen-4bit" in _text(ctx)


def test_pull_previews_before_transferring(ctx, _no_network_pull, monkeypatch):
    from luxe import modelstore as ms

    monkeypatch.setattr(ms, "local_model_names", lambda *a, **k: [])
    cmd.dispatch("/pull org/Champ", ctx)
    out = _text(ctx)
    assert "HuggingFace" in out
    assert "--yes" in out                       # consent step is explicit
    assert _FakeAdmin.started == []             # nothing transferred yet


def test_pull_yes_starts_the_download(ctx, _no_network_pull, monkeypatch):
    from luxe import modelstore as ms

    monkeypatch.setattr(ms, "local_model_names", lambda *a, **k: [])
    cmd.dispatch("/pull org/Champ --yes", ctx)
    assert _FakeAdmin.started == ["org/Champ"]
    assert "downloaded" in _text(ctx)


def test_pull_refuses_a_model_already_in_the_store(ctx, _no_network_pull, monkeypatch):
    from luxe import modelstore as ms

    monkeypatch.setattr(ms, "local_model_names", lambda *a, **k: ["Champ"])
    cmd.dispatch("/pull org/Champ --yes", ctx)
    assert "--force" in _text(ctx)
    assert _FakeAdmin.started == []


def test_pull_reports_when_there_is_no_source(ctx, _no_network_pull):
    cmd.dispatch("/pull JustAName --yes", ctx)
    assert "Nowhere to pull" in _text(ctx)


def test_pull_from_a_local_dir_imports_it(ctx, _no_network_pull, tmp_path, monkeypatch):
    from luxe import modelstore as ms

    src = tmp_path / "nas" / "Champ"
    src.mkdir(parents=True)
    (src / "config.json").write_text("{}")
    (src / "model.safetensors").write_bytes(b"\0" * 256)
    store = tmp_path / "store"
    monkeypatch.setattr(ms, "DEFAULT_MODELS_DIR", store)
    monkeypatch.setattr(ms, "local_model_names", lambda *a, **k: [])

    cmd.dispatch(f"/pull Champ --from {src} --yes", ctx)

    assert (store / "Champ" / "config.json").exists()
    assert "✓" in _text(ctx)


def test_pull_surfaces_admin_errors(ctx, monkeypatch):
    from luxe import modelstore as ms

    class Broken(_FakeAdmin):
        def search(self, query, **k):
            raise ms.ModelStoreError("oMLX admin unreachable at http://x")

    monkeypatch.setattr(ms, "OmlxAdmin", Broken)
    cmd.dispatch("/pull --search Qwen", ctx)
    assert "unreachable" in _text(ctx)


# --- /theme /tools /status /unload /retry ------------------------------------


def test_theme_lists_palettes_and_marks_active(ctx):
    from luxe.chat import theme as theme_mod

    theme_mod.set_palette("auto")
    cmd.dispatch("/theme", ctx)
    out = _text(ctx)
    for name in theme_mod.list_palettes():
        assert name in out
    assert "active" in out


def test_theme_switches_the_palette(ctx):
    from luxe.chat import theme as theme_mod

    try:
        cmd.dispatch("/theme cool", ctx)
        assert theme_mod.active_palette() == "cool"
        cmd.dispatch("/theme auto", ctx)
        assert theme_mod.active_palette() == "auto"
    finally:
        theme_mod.set_palette(None)


def test_theme_rejects_unknown_palette(ctx):
    from luxe.chat import theme as theme_mod

    cmd.dispatch("/theme neon", ctx)
    assert "Unknown palette" in _text(ctx)
    assert theme_mod.active_palette() == "auto"


def test_tools_separates_active_from_read_only_gated(ctx):
    ctx.slots.cfg.roles["monolith"].tools = ["read_file", "grep", "write_file",
                                             "edit_file", "bash"]
    cmd.dispatch("/tools", ctx)
    out = _text(ctx)
    assert "read_file" in out and "grep" in out
    assert "Gated by read-only mode" in out
    assert "/write" in out


def test_tools_shows_everything_in_write_mode(ctx):
    ctx.slots.cfg.roles["monolith"].tools = ["read_file", "write_file", "bash"]
    ctx.session.write_enabled = True
    cmd.dispatch("/tools", ctx)
    out = _text(ctx)
    assert "Gated by read-only mode" not in out
    assert "allowlisted" in out          # bash mode is spelled out


def test_status_reports_the_session(ctx):
    cmd.dispatch("/status", ctx)
    out = _text(ctx)
    for key in ("repo", "backend", "model", "weights", "mode", "turns"):
        assert key in out


def test_unload_frees_ram_and_forgets_residency(ctx, monkeypatch):
    unloaded: list = []
    monkeypatch.setattr(ctx.slots.backend, "loaded_models", lambda: ["Champ"],
                        raising=False)
    monkeypatch.setattr(ctx.slots.backend, "unload_all_loaded",
                        lambda **k: (unloaded.append("Champ"), {"Champ": True})[1],
                        raising=False)
    ctx.slots._resident = "Champ"

    cmd.dispatch("/unload", ctx)

    assert unloaded == ["Champ"]
    assert ctx.slots.resident == ""          # next turn reloads
    assert "unloaded Champ" in _text(ctx)


def test_unload_with_nothing_loaded(ctx, monkeypatch):
    monkeypatch.setattr(ctx.slots.backend, "loaded_models", lambda: [], raising=False)
    cmd.dispatch("/unload", ctx)
    assert "nothing loaded" in _text(ctx)


def test_retry_resubmits_the_last_user_message(ctx):
    from luxe.chat.session import ChatTurn

    ctx.session.add_turn(ChatTurn(user="fix the parser", assistant="done"))
    res = cmd.dispatch("/retry", ctx)
    assert res.submit == "fix the parser"
    assert "retrying" in _text(ctx)


def test_retry_with_no_history(ctx):
    res = cmd.dispatch("/retry", ctx)
    assert res.submit == ""
    assert "Nothing to retry" in _text(ctx)


# --- /doctor /diff /export ---------------------------------------------------


def _init_repo(path):
    import subprocess
    subprocess.run(["git", "init", "-q", "."], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "a.py").write_text("one\ntwo\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def test_doctor_prints_a_verdict_line(ctx, monkeypatch):
    from luxe.chat import origin as origin_mod

    monkeypatch.setattr(origin_mod, "network_mounts", lambda **k: [])
    cmd.dispatch("/doctor", ctx)
    out = _text(ctx)
    assert "Doctor" in out
    assert "oMLX endpoint" in out and "mode" in out
    # a verdict is always printed, whichever way it went
    assert ("all clear" in out or "caveats" in out or "broken" in out)


def test_diff_reports_no_changes_on_a_clean_tree(ctx):
    _init_repo(Path(ctx._repo))
    cmd.dispatch("/diff", ctx)
    assert "no changes" in _text(ctx)


def test_diff_lists_counts_and_untracked(ctx):
    repo = Path(ctx._repo)
    _init_repo(repo)
    (repo / "a.py").write_text("one\ntwo\nthree\n")
    (repo / "new.py").write_text("x\n")

    cmd.dispatch("/diff", ctx)
    out = _text(ctx)

    assert "a.py" in out and "+1" in out
    assert "new.py" in out and "untracked" in out
    assert "--full" in out                    # hint to see the patch


def test_diff_full_prints_the_patch(ctx):
    repo = Path(ctx._repo)
    _init_repo(repo)
    (repo / "a.py").write_text("one\nCHANGED\n")

    cmd.dispatch("/diff --full", ctx)
    out = _text(ctx)

    assert "+CHANGED" in out and "-two" in out


def test_diff_scopes_to_session_files_when_the_ledger_has_them(ctx, monkeypatch):
    from luxe.state import ledger as ledger_mod

    repo = Path(ctx._repo)
    _init_repo(repo)
    (repo / "a.py").write_text("one\nedited\n")
    (repo / "untouched.py").write_text("noise\n")
    ctx.session.session_id = "sess1"
    monkeypatch.setattr(ledger_mod, "load",
                        lambda sid: ledger_mod.Ledger(files=["a.py"]))

    cmd.dispatch("/diff", ctx)
    out = _text(ctx)

    assert "this session" in out and "a.py" in out
    assert "untouched.py" not in out


def test_diff_without_a_repo(ctx):
    ctx.session.repo_path = ""
    cmd.dispatch("/diff", ctx)
    assert "No repo" in _text(ctx)


def test_export_writes_markdown(ctx):
    from luxe.chat.session import ChatTurn
    from luxe.memory import session as session_store

    meta = session_store.new_session(repo_path=ctx._repo)
    ctx.session.session_id = meta.session_id
    session_store.append_turn(meta.session_id, "user", text="hello there",
                              slot="chat")
    session_store.append_turn(meta.session_id, "assistant", text="hi", steps=1)
    ctx.session.add_turn(ChatTurn(user="hello there", assistant="hi"))

    cmd.dispatch("/export", ctx)
    out = _text(ctx)

    assert "exported" in out
    written = session_store.session_dir(meta.session_id) / "transcript.md"
    assert "hello there" in written.read_text()


def test_export_to_an_explicit_path(ctx, tmp_path):
    from luxe.memory import session as session_store

    meta = session_store.new_session(repo_path=ctx._repo)
    ctx.session.session_id = meta.session_id
    session_store.append_turn(meta.session_id, "user", text="q", slot="chat")
    dest = tmp_path / "sub" / "chat.md"

    cmd.dispatch(f"/export {dest}", ctx)

    assert dest.is_file()
    assert "chat.md" in _text(ctx)      # (console wraps the full path at width)


def test_export_before_a_session_exists(ctx):
    ctx.session.session_id = ""
    cmd.dispatch("/export", ctx)
    assert "Nothing to export" in _text(ctx)


def test_export_reports_an_unwritable_destination(ctx, tmp_path):
    from luxe.memory import session as session_store

    meta = session_store.new_session(repo_path=ctx._repo)
    ctx.session.session_id = meta.session_id
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")

    cmd.dispatch(f"/export {blocker / 'out.md'}", ctx)

    assert "cannot write export" in _text(ctx)


# --- /project and /index ------------------------------------------------------


def test_project_with_no_args_shows_what_is_attached(ctx):
    ctx.session.project_kind = "none"
    cmd.dispatch("/project", ctx)
    out = _text(ctx)
    assert "no project" in out
    assert "bm25_search" in out and "find_symbol" in out
    assert "/index" in out                      # the escape hatch is offered


def test_project_shows_a_git_session_as_attached(ctx):
    ctx.session.project_kind = "git"
    cmd.dispatch("/project", ctx)
    assert "git repo" in _text(ctx)


def test_project_switch_calls_the_hook_and_repoints_the_session(ctx, tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    calls: list = []

    def _hook(path):
        calls.append(path)
        return {"root": str(target), "kind": "git", "label": "git repo",
                "files": 42, "symbols": 100, "truncated": "", "used_git": True}

    ctx.on_project = _hook
    cmd.dispatch(f"/project {target}", ctx)

    assert calls == [str(target)]
    assert ctx.session.repo_path == str(target)
    assert ctx.session.project_kind == "git"
    out = _text(ctx)
    assert "42 files" in out and "git-tracked" in out


def test_project_switch_to_a_non_project_says_so(ctx, tmp_path):
    plain = tmp_path / "notes"
    plain.mkdir()
    ctx.on_project = lambda p: {"root": str(plain), "kind": "none",
                                "label": "no project", "files": 0,
                                "symbols": 0, "truncated": "", "used_git": False}

    cmd.dispatch(f"/project {plain}", ctx)

    assert "isn't a project" in _text(ctx)
    assert ctx.session.project_kind == "none"


def test_project_rejects_a_missing_directory(ctx):
    called: list = []
    ctx.on_project = lambda p: called.append(p)
    cmd.dispatch("/project /definitely/not/here", ctx)
    assert "not a directory" in _text(ctx)
    assert called == []


def test_project_reports_a_held_lock_and_stays_put(ctx, tmp_path):
    from luxe.locks import LockHeld

    target = tmp_path / "busy"
    target.mkdir()
    ctx.session.repo_path = "/original"

    from luxe.locks import LockInfo

    def _hook(path):
        raise LockHeld(LockInfo(pid=999, run_id="chat-123", started_at=0.0,
                                repo_path="/busy"), Path("/tmp/x.lock"))

    ctx.on_project = _hook
    cmd.dispatch(f"/project {target}", ctx)

    assert "another luxe run is active" in _text(ctx)
    assert ctx.session.repo_path == "/original"      # unchanged


def test_project_without_a_hook_is_explained(ctx, tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    ctx.on_project = None
    cmd.dispatch(f"/project {d}", ctx)
    assert "can't switch projects" in _text(ctx)


def test_index_builds_here_by_default(ctx):
    calls: list = []

    def _hook(path):
        calls.append(path)
        return {"root": "/here", "kind": "dir", "label": "project (pyproject.toml)",
                "files": 7, "symbols": 12, "truncated": "", "used_git": False}

    ctx.on_project = _hook
    cmd.dispatch("/index", ctx)

    assert calls == [None]                       # None = "resolve where I am"
    assert "7 files" in _text(ctx)


def test_index_reports_truncation(ctx):
    ctx.on_project = lambda p: {"root": "/here", "kind": "dir", "label": "project",
                                "files": 8000, "symbols": 1, "used_git": False,
                                "truncated": "8000-file cap"}
    cmd.dispatch("/index", ctx)
    assert "truncated" in _text(ctx) and "8000-file cap" in _text(ctx)
