"""Tests for chat/project.py + no-project mode — starting `luxe chat` anywhere.

A session may be about a git repo, a marker-bearing directory, or nothing at
all. The "nothing" case must be first-class: no index built (indexing `$HOME`
cost 210s for coverage the model can't rely on), index-backed tools withheld
rather than failing per call, and `/project` / `/index` to opt in later.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe.chat import project as project_mod
from luxe.chat.session import ChatSession


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "."], cwd=path, check=True)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """Pin $HOME inside tmp_path so resolution never sees the real home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


# --- resolution -------------------------------------------------------------


class TestResolve:
    def test_git_repo_resolves_to_its_root(self, _home):
        repo = _home / "code" / "proj"
        _git_init(repo)
        (repo / "src").mkdir()

        p = project_mod.resolve(repo / "src")

        assert p.kind == project_mod.GIT
        assert Path(p.root) == repo.resolve()      # the ROOT, not the subdir
        assert p.is_project and p.label == "git repo"

    def test_marker_directory_without_git(self, _home):
        d = _home / "scratchpad"
        d.mkdir()
        (d / "pyproject.toml").write_text("[project]\n")

        p = project_mod.resolve(d)

        assert p.kind == project_mod.DIR
        assert p.marker == "pyproject.toml"
        assert p.is_project

    def test_plain_directory_is_no_project(self, _home):
        d = _home / "notes"
        d.mkdir()
        (d / "todo.txt").write_text("x")

        p = project_mod.resolve(d)

        assert p.kind == project_mod.NONE
        assert p.is_project is False
        assert p.label == "no project"
        assert Path(p.root) == d.resolve()         # root is still cwd for tools

    def test_home_itself_is_never_the_project(self, _home):
        """Even a dotfiles-style repo at `$HOME` must not become the project —
        indexing a home directory is the thing this avoids."""
        _git_init(_home)
        assert project_mod.resolve(_home).kind == project_mod.NONE

    def test_never_walks_above_home(self, _home, tmp_path):
        """A repo ABOVE home (tmp_path here) is not 'the project you're in'."""
        _git_init(tmp_path)
        d = _home / "loose"
        d.mkdir()

        p = project_mod.resolve(d)

        assert p.kind == project_mod.NONE

    def test_missing_path_degrades_to_none(self, _home):
        p = project_mod.resolve(_home / "nope")
        assert p.kind == project_mod.NONE

    def test_file_path_degrades_to_none(self, _home):
        f = _home / "a.txt"
        f.write_text("x")
        assert project_mod.resolve(f).kind == project_mod.NONE

    @pytest.mark.parametrize("marker", ["package.json", "Cargo.toml", "go.mod",
                                        "Makefile", ".luxe"])
    def test_each_marker_counts(self, _home, marker):
        d = _home / f"p-{marker}"
        d.mkdir()
        (d / marker).write_text("x")
        assert project_mod.resolve(d).kind == project_mod.DIR


# --- tool surface -----------------------------------------------------------


class TestIndexToolGating:
    @pytest.fixture(autouse=True)
    def _clear_indexes(self):
        from luxe import search as search_mod
        from luxe import symbols as symbols_mod

        search_mod.reset_index()
        symbols_mod.reset_index()
        yield
        search_mod.reset_index()
        symbols_mod.reset_index()

    def _role(self):
        from luxe.config import RoleConfig
        return RoleConfig(model_key="monolith",
                          tools=["read_file", "grep", "bm25_search",
                                 "find_symbol", "write_file"])

    def test_index_tools_are_withheld_without_an_index(self):
        from luxe.chat import repl

        role = repl._drop_unavailable_index_tools(self._role())

        assert "bm25_search" not in role.tools
        assert "find_symbol" not in role.tools
        assert "read_file" in role.tools and "grep" in role.tools

    def test_index_tools_return_once_indexes_exist(self, tmp_path):
        from luxe import search as search_mod
        from luxe import symbols as symbols_mod
        from luxe.chat import repl

        (tmp_path / "a.py").write_text("class Foo:\n    pass\n")
        search_mod.set_index(search_mod.build_bm25_index(tmp_path))
        symbols_mod.set_index(symbols_mod.build_symbol_index(tmp_path))

        role = repl._drop_unavailable_index_tools(self._role())

        assert "bm25_search" in role.tools and "find_symbol" in role.tools

    def test_reported_availability_tracks_the_resident_indexes(self, tmp_path):
        from luxe import search as search_mod
        from luxe.chat import repl

        assert repl.index_tools_available() == {"bm25_search": False,
                                               "find_symbol": False}
        (tmp_path / "a.py").write_text("x = 1\n")
        search_mod.set_index(search_mod.build_bm25_index(tmp_path))
        assert repl.index_tools_available()["bm25_search"] is True


# --- prompt framing ---------------------------------------------------------


class TestNoProjectPrompt:
    def test_no_project_session_tells_the_model_there_is_no_index(self):
        from luxe.agents.prompts import NO_PROJECT_CHAT_HINT

        s = ChatSession(project_kind="none", write_enabled=True)
        ctx, _ = s.build_extra_context("what is a monad?")

        assert NO_PROJECT_CHAT_HINT in ctx
        assert "/project" in ctx
        # And it must not claim luxe lacks search — just that it isn't indexed.
        assert "lacks code search" in NO_PROJECT_CHAT_HINT

    def test_project_session_gets_no_such_hint(self):
        from luxe.agents.prompts import NO_PROJECT_CHAT_HINT

        s = ChatSession(project_kind="git", write_enabled=True)
        ctx, _ = s.build_extra_context("fix the parser")
        assert NO_PROJECT_CHAT_HINT not in ctx

    def test_both_hints_coexist_in_read_only_no_project(self):
        from luxe.agents.prompts import NO_PROJECT_CHAT_HINT, READ_ONLY_CHAT_HINT

        s = ChatSession(project_kind="none", write_enabled=False)
        ctx, _ = s.build_extra_context("hello")
        assert NO_PROJECT_CHAT_HINT in ctx and READ_ONLY_CHAT_HINT in ctx
        assert ctx.count("<session_mode>") == 1     # one block, not two


# --- status bar -------------------------------------------------------------


def test_status_bar_shows_no_project_when_unattached(monkeypatch):
    from luxe.chat import slots as slots_mod
    from luxe.chat.status import StatusState, fields
    from luxe.config import PipelineConfig, RoleConfig

    class _B:
        base_url = ""
        api_key = ""

        def __init__(self, **k):
            pass

    monkeypatch.setattr(slots_mod, "Backend", _B)
    cfg = PipelineConfig(models={"monolith": "C"},
                         roles={"monolith": RoleConfig(model_key="monolith")})
    sm = slots_mod.SlotManager(cfg)

    segs = fields(ChatSession(project_kind="none"), sm, "", StatusState())
    text = " · ".join("".join(t for t, _p, _r in s.spans) for s in segs)
    assert "no project" in text

    segs = fields(ChatSession(project_kind="git"), sm, "", StatusState())
    text = " · ".join("".join(t for t, _p, _r in s.spans) for s in segs)
    assert "no project" not in text


def test_repo_outside_home_is_still_a_project(_home, tmp_path):
    """The home guard must not reject legitimate projects that live elsewhere
    (an external volume, /opt, a work mount)."""
    outside = tmp_path / "volumes" / "work" / "proj"
    _git_init(outside)

    p = project_mod.resolve(outside)

    assert p.kind == project_mod.GIT
    assert Path(p.root) == outside.resolve()


# --- cli integration: startup mode + mid-session attach ----------------------


def _run_chat_cmd(monkeypatch, start_dir, front_end):
    """Invoke `luxe chat` with the front-end replaced by `front_end(**kwargs)`,
    so the real cli wiring (resolve → index → lock → hooks) is exercised."""
    from click.testing import CliRunner

    import luxe.chat as chat_pkg
    from luxe import cli as cli_mod

    monkeypatch.setattr(chat_pkg, "run_chat_repl", front_end)
    monkeypatch.setattr(cli_mod.sys.stdout, "isatty", lambda: False, raising=False)
    result = CliRunner().invoke(
        cli_mod.chat_cmd, ["--repo", str(start_dir), "--keep-loaded"])
    assert result.exit_code == 0, result.output
    return result.output


def test_starting_in_a_plain_directory_builds_no_index(_home, monkeypatch):
    from luxe import search as search_mod
    from luxe import symbols as symbols_mod

    plain = _home / "notes"
    plain.mkdir()
    (plain / "todo.md").write_text("# todo\n")
    seen = {}

    def front_end(cfg, repo_path, languages, **kw):
        seen["kind"] = kw.get("project_kind")
        seen["bm25"] = search_mod.get_index()
        seen["symbols"] = symbols_mod.get_index()
        seen["languages"] = languages

    out = _run_chat_cmd(monkeypatch, plain, front_end)

    assert seen["kind"] == "none"
    assert seen["bm25"] is None and seen["symbols"] is None
    assert seen["languages"] == frozenset()
    assert "no project" in out


def test_starting_in_a_repo_subdir_indexes_the_whole_repo(_home, monkeypatch):
    from luxe import search as search_mod

    repo = _home / "proj"
    _git_init(repo)
    (repo / "top.py").write_text("def top(): pass\n")
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    (sub / "inner.py").write_text("def inner(): pass\n")
    seen = {}

    def front_end(cfg, repo_path, languages, **kw):
        seen["root"] = repo_path
        seen["kind"] = kw.get("project_kind")
        seen["paths"] = list(search_mod.get_index().paths)

    _run_chat_cmd(monkeypatch, sub, front_end)

    assert Path(seen["root"]) == repo.resolve()      # walked UP to the git root
    assert seen["kind"] == "git"
    assert "top.py" in seen["paths"]                 # whole repo, not the subdir
    assert "src/deep/inner.py" in seen["paths"]


def test_attaching_a_project_mid_session_indexes_and_locks(_home, monkeypatch):
    """The `/project <path>` hook: index appears, the tool surface opens up, and
    the repo lock is now held for the new project."""
    from luxe import search as search_mod
    from luxe.chat import repl as repl_mod
    from luxe.locks import LockHeld, acquire_repo_lock

    plain = _home / "elsewhere"
    plain.mkdir()
    repo = _home / "proj"
    _git_init(repo)
    (repo / "a.py").write_text("class Alpha:\n    pass\n")
    captured = {}

    def front_end(cfg, repo_path, languages, **kw):
        attach = kw["on_project"]
        # Before: unattached, no index, index tools withheld.
        assert search_mod.get_index() is None
        assert repl_mod.index_tools_available() == {"bm25_search": False,
                                                   "find_symbol": False}
        captured["summary"] = attach(str(repo))
        captured["after"] = repl_mod.index_tools_available()
        captured["paths"] = list(search_mod.get_index().paths)
        try:
            with acquire_repo_lock(str(repo), "other"):
                captured["lock_held"] = False
        except LockHeld:
            captured["lock_held"] = True

    _run_chat_cmd(monkeypatch, plain, front_end)

    assert captured["summary"]["kind"] == "git"
    assert Path(captured["summary"]["root"]) == repo.resolve()
    assert captured["summary"]["files"] == 1
    assert captured["after"] == {"bm25_search": True, "find_symbol": True}
    assert captured["paths"] == ["a.py"]
    assert captured["lock_held"] is True


def test_attach_to_a_plain_directory_leaves_the_session_unindexed(_home, monkeypatch):
    from luxe import search as search_mod

    start = _home / "one"
    start.mkdir()
    other = _home / "two"
    other.mkdir()
    captured = {}

    def front_end(cfg, repo_path, languages, **kw):
        captured["summary"] = kw["on_project"](str(other))
        captured["index"] = search_mod.get_index()

    _run_chat_cmd(monkeypatch, start, front_end)

    assert captured["summary"]["kind"] == "none"
    assert captured["index"] is None      # nothing indexed, nothing pretended
