"""Regressions for the 2026-09 gitkit review: cache staleness, the apply
executor's revert/keep/verify paths, report parsing, and ephemeral leaks.

Every repro builds a throwaway git repo under tmp_path with HOME pointed at a
temp dir — nothing here may touch the real ~/.luxe.
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from rich.console import Console


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LUXE_HOME", raising=False)
    return home


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True).stdout.strip()


def _init_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@e.com")
    _git(root, "config", "user.name", "T")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


# --- 3: severity headers as real reports write them --------------------------

_REAL_AUDIT = """# Repository audit
**Findings: 4**

## Repository summary & risk
Use-risk: low.

## Bugs & security

### Critical
- **critical** `a.py:1` — takeover

### High
- `b.py:2` — token bypass

### M — `StopIteration` on bare `next()`
Evidence:
```python
# audio_meta.py:29
x = next(it)
```

### Low
- `d.py:4` — tidy
- `e.py:5` — nit

## Structural improvements
- split the god module
"""


def test_extract_findings_reads_nested_and_shorthand_severity_headers():
    from luxe.gitkit import store
    out = store.extract_findings(_REAL_AUDIT)
    assert "takeover" in out and "token bypass" in out
    assert "StopIteration" in out and "audio_meta.py:29" in out
    assert "tidy" in out
    assert "Repository summary" not in out
    assert "split the god module" not in out


def test_min_severity_hides_nested_sections():
    from luxe.gitkit import store
    out, dropped = store.filter_min_severity(_REAL_AUDIT, "high")
    assert "takeover" in out and "token bypass" in out
    assert "StopIteration" not in out           # ### M — … (one finding)
    assert "tidy" not in out and "nit" not in out
    assert "split the god module" in out        # later ## section survives
    assert dropped == 3


# --- apply executor (2, 4, 5, 6, 7) ------------------------------------------

def _tty_console():
    return Console(file=io.StringIO(), force_terminal=True, width=160,
                   color_system=None, highlight=False)


@pytest.fixture
def _cfg():
    from luxe.config import PipelineConfig, RoleConfig
    return PipelineConfig(models={"monolith": "Champ"},
                          roles={"monolith": RoleConfig(model_key="monolith")})


@pytest.fixture
def apply_env(monkeypatch):
    from luxe.gitkit import apply

    class _FB:
        def __init__(self, *a, **k):
            self.model = "Champ"
    monkeypatch.setattr("luxe.backend.Backend", _FB)
    monkeypatch.setattr(apply, "_is_tty", lambda c: True)
    return apply


def _save_plan(repo: Path, steps: list[dict]) -> None:
    from luxe.gitkit import health, plan
    p = plan.normalize_plan({"summary": "t", "steps": steps},
                            head=health.current_head(repo))
    plan.save_plan_json(repo, p)


def _step(sid: str, verify: str = "preserve behavior", **kw) -> dict:
    return {"id": sid, "title": f"step {sid}", "target_files": ["main.py"],
            "change": {"op": "rename", "detail": "d"}, "risk": "low",
            "verify": verify, "depends_on": [], **kw}


class _R:
    final_text = "done"


def _stub(repo: Path, writes: dict[str, dict[str, str]],
          raise_on: dict | None = None):
    """run_single stub: step id -> {path: content} written into the repo."""
    calls: list[str] = []

    def fake(backend, role, *, run_id="", **kw):
        sid = run_id.replace("gitchange-apply-", "")
        calls.append(sid)
        for rel, text in writes.get(sid, {}).items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text)
        if raise_on and sid in raise_on:
            raise raise_on[sid]
        return _R()
    return fake, calls


def _repo(tmp_path: Path) -> Path:
    return _init_repo(tmp_path / "repo", {"main.py": "def f():\n    return 1\n"})


def test_discarded_new_file_is_not_committed_by_next_kept_step(
        tmp_path, _cfg, apply_env):
    """2: `add -N .` staged the discarded step's new file as intent-to-add;
    `checkout -- .` left it as an EMPTY file the next keep committed."""
    repo = _repo(tmp_path)
    _save_plan(repo, [_step("S1"), _step("S2")])
    fake, _ = _stub(repo, {"S1": {"newmod.py": "x = 1\n"},
                           "S2": {"main.py": "def f():\n    return 2\n"}})
    answers = iter(["discard", "keep"])
    rc = apply_env.run_apply(repo_path=str(repo), cfg=_cfg,
                             console=_tty_console(),
                             reader=lambda _p: next(answers), run_single_fn=fake)
    assert rc == 0
    committed = _out(repo, "show", "--name-only", "--pretty=", "HEAD").split()
    assert committed == ["main.py"]
    assert not (repo / "newmod.py").exists()
    assert _out(repo, "status", "--porcelain") == ""


def test_failed_commit_is_not_reported_kept(tmp_path, _cfg, apply_env):
    """4: a failing hook used to leave the step "kept" but uncommitted."""
    repo = _repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    _save_plan(repo, [_step("S1"), _step("S2")])
    fake, calls = _stub(repo, {"S1": {"main.py": "def f():\n    return 2\n"},
                               "S2": {"other.py": "y = 2\n"}})
    con = _tty_console()
    apply_env.run_apply(repo_path=str(repo), cfg=_cfg, console=con,
                        reader=lambda _p: "keep", run_single_fn=fake)
    out = con.file.getvalue()
    assert "kept=0" in out and "failed=1" in out
    assert "lint says no" in out
    assert calls == ["S1"]                        # stopped; S2 never ran
    assert _out(repo, "log", "-1", "--pretty=%s") == "init"
    # the operator's work is still there to commit, not silently wiped
    assert "return 2" in (repo / "main.py").read_text()


def test_mirror_does_not_block_or_pollute_apply(tmp_path, _cfg, apply_env):
    """5: an untracked .luxe/gitkit/ mirror made --apply refuse "dirty", and
    `add -A` then committed it with the step."""
    repo = _repo(tmp_path)
    mirror = repo / ".luxe" / "gitkit"
    mirror.mkdir(parents=True)
    (mirror / "README.md").write_text("mirror\n")
    _save_plan(repo, [_step("S1")])
    fake, calls = _stub(repo, {"S1": {"main.py": "def f():\n    return 2\n"}})
    rc = apply_env.run_apply(repo_path=str(repo), cfg=_cfg,
                             console=_tty_console(), reader=lambda _p: "keep",
                             run_single_fn=fake)
    assert rc == 0 and calls == ["S1"]
    committed = _out(repo, "show", "--name-only", "--pretty=", "HEAD").split()
    assert committed == ["main.py"]
    assert (mirror / "README.md").exists()


def test_other_luxe_files_still_count_as_dirty(tmp_path, apply_env):
    """5: the exemption is exactly .luxe/gitkit/, not everything under .luxe."""
    repo = _repo(tmp_path)
    (repo / ".luxe").mkdir()
    (repo / ".luxe" / "memory.md").write_text("notes\n")
    assert apply_env._require_clean(repo, _tty_console(), "now") is False


def test_failed_git_status_counts_as_dirty(tmp_path, apply_env):
    notrepo = tmp_path / "plain"
    notrepo.mkdir()
    assert apply_env._require_clean(notrepo, _tty_console(), "now") is False


def test_abort_from_detached_head_restores_it(tmp_path, _cfg, apply_env,
                                              monkeypatch):
    """6: `checkout HEAD` on abort was a no-op — the repo stayed on the
    gitchange branch and the branch delete failed."""
    repo = _repo(tmp_path)
    sha = _out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--detach", sha)
    monkeypatch.setattr("luxe.gitkit.run_git_report", lambda kind, **kw: None)
    fake, calls = _stub(repo, {})
    rc = apply_env.run_apply(repo_path=str(repo), cfg=_cfg,
                             console=_tty_console(), reader=lambda _p: "keep",
                             run_single_fn=fake)
    assert rc == 1 and not calls                 # no plan → abort path
    assert _out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert _out(repo, "rev-parse", "HEAD") == sha
    assert _out(repo, "branch", "--list", "gitchange/*") == ""


def test_ctrl_c_mid_step_reverts_the_step(tmp_path, _cfg, apply_env):
    """6: KeyboardInterrupt escaped the step loop with the partial writes
    still in the tree."""
    repo = _repo(tmp_path)
    _save_plan(repo, [_step("S1"), _step("S2")])
    fake, calls = _stub(repo, {"S1": {"main.py": "def f():\n    return 2\n"},
                               "S2": {"half.py": "partial\n"}},
                        raise_on={"S2": KeyboardInterrupt()})
    rc = apply_env.run_apply(repo_path=str(repo), cfg=_cfg,
                             console=_tty_console(), reader=lambda _p: "keep",
                             run_single_fn=fake)
    assert rc == 130 and calls == ["S1", "S2"]
    assert "gitchange S1" in _out(repo, "log", "-1", "--pretty=%s")
    assert not (repo / "half.py").exists()
    assert _out(repo, "status", "--porcelain") == ""


def test_verify_command_needs_confirmation(tmp_path, _cfg, apply_env):
    """7: the model-written verify string ran via bash -lc unconfirmed."""
    repo = _repo(tmp_path)
    marker = tmp_path / "ran"
    _save_plan(repo, [_step("S1", verify=f"python3 -c \"open('{marker}','w')\"")])
    fake, _ = _stub(repo, {"S1": {"main.py": "def f():\n    return 2\n"}})
    prompts_seen: list[str] = []

    def reader(p):
        prompts_seen.append(p)
        return "" if "run it?" in p else "keep"   # default answer = N
    con = _tty_console()
    apply_env.run_apply(repo_path=str(repo), cfg=_cfg, console=con,
                        reader=reader, run_single_fn=fake)
    assert any("run it?" in p for p in prompts_seen)
    assert "python3 -c" in con.file.getvalue()    # the command was SHOWN
    assert not marker.exists()                    # …and not run by default


def test_verify_runs_after_yes_and_prose_is_advisory(tmp_path, apply_env):
    marker = tmp_path / "ran"
    con = _tty_console()
    ok, _ = apply_env._run_verify(f"python3 -c \"open('{marker}','w')\"",
                                  tmp_path, 30, console=con,
                                  reader=lambda _p: "y")
    assert ok is True and marker.exists()
    asked: list[str] = []
    ok, _ = apply_env._run_verify("preserve the latest behavior", tmp_path, 30,
                                  console=con,
                                  reader=lambda p: asked.append(p) or "y")
    assert ok is None and not asked               # first token, not substring


# --- structural: one repo-root/index swap -------------------------------------

def test_indexed_target_restores_an_unset_root(tmp_path, monkeypatch):
    from luxe import search, symbols
    from luxe.gitkit.workspace import indexed_target
    from luxe.tools import fs
    repo = _init_repo(tmp_path / "r", {"a.py": "def a():\n    return 1\n"})
    monkeypatch.setattr(fs, "_REPO_ROOT", None)
    search.reset_index()
    symbols.reset_index()
    with indexed_target(str(repo)) as swapped:
        assert swapped and fs.get_repo_root() == repo.resolve()
        assert search.get_index() is not None
    assert fs.get_repo_root() is None
    assert search.get_index() is None and symbols.get_index() is None
    # a resident root that already IS the target is reused, not rebuilt
    fs.set_repo_root(repo)
    with indexed_target(str(repo.resolve())) as swapped:
        assert swapped is False


# --- deep cache (1, 13, 16, 20) -----------------------------------------------

class _Res:
    def __init__(self, text: str, aborted: bool = False):
        self.final_text = text
        self.aborted = aborted
        self.wall_s = 1.0
        self.completion_tokens = 1
        self.steps = 1
        self.tool_calls_total = 0


def _deep_cfg():
    from luxe.config import PipelineConfig, RoleConfig
    return PipelineConfig(models={"monolith": "Champ"},
                          roles={"monolith": RoleConfig(model_key="monolith")})


def _bug_repo(tmp_path: Path) -> Path:
    files = {
        "auth/login.py": "def login():\n    pass  # BUG: token-not-checked\n"
                         + "x = 1\n" * 30,
        "core/engine.py": "def run():\n    pass  # BUG: leak-in-engine\n"
                          + "y = 2\n" * 30,
        "web/index.js": "export const a = 1;\n" + "// pad\n" * 30,
    }
    for i in range(8):
        files[f"core/pad{i}.py"] = f"p{i} = {i}\n" + "w = 0\n" * 30
    return _init_repo(tmp_path / "bugrepo", files)


def _stage(run_id: str) -> str:
    for s in ("survey", "synthesis", "reduce", "format", "chunk"):
        if s in run_id:
            return s
    return "?"


def _bug_stub(repo: Path, calls: list[str], *, empty: bool = False):
    """Chunk passes report one finding per BUG: marker in the chunk's files,
    read from the WORKING TREE at call time (as the real agent's tools do)."""
    import json as _json
    import re as _re

    def fake(backend, role_cfg, *, run_id="", extra_context="", **kw):
        stage = _stage(run_id)
        calls.append(stage)
        if stage == "survey":
            return _Res("Survey notes: python app.")
        if stage == "synthesis":
            return _Res("# Repository audit\n**Findings: n**\nok")
        if empty:
            return _Res("")
        body = extra_context.split("<chunk_files>")[1].split("Symbols defined")[0]
        findings = []
        for rel in _re.findall(r"^- (.+)$", body, _re.M):
            p = repo / rel.strip()
            if p.is_file():
                for ln in p.read_text().splitlines():
                    if "BUG:" in ln:
                        findings.append({"title": ln.split("BUG:")[1].strip(),
                                         "severity": "high",
                                         "evidence": [f"{rel.strip()}:2"]})
        return _Res("```json\n" + _json.dumps({"findings": findings}) + "\n```")
    return fake


def _run_deep(repo: Path, monkeypatch, calls: list[str], **kw):
    import luxe.agents.single as single_mod
    from luxe.gitkit import deep, run_git_report

    class _FB:
        def __init__(self, *a, **k):
            self.model = "Champ"
    monkeypatch.setattr("luxe.backend.Backend", _FB)
    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    stub = kw.pop("stub", None) or _bug_stub(repo, calls)
    monkeypatch.setattr(single_mod, "run_single", stub)
    con = Console(file=io.StringIO(), force_terminal=False, width=200)
    run_git_report("gitaudit", cfg=_deep_cfg(), repo_path=repo, console=con,
                   save=True, deep=True, mirror=False, **kw)
    return con.file.getvalue()


def _xref_titles(repo: Path) -> set[str]:
    import json as _json
    from luxe.gitkit import store
    work = max(store.reports_dir(repo).glob("gitaudit-*.work"),
               key=lambda p: (p / "xref.json").stat().st_mtime_ns)
    xref = _json.loads((work / "xref.json").read_text())
    return {f["title"] for f in xref["provisional_findings"]}


def test_uncommitted_edit_invalidates_cached_note(tmp_path, monkeypatch):
    """1: note validation compared HEAD blob shas while the chunk pass reads
    the working tree — an uncommitted fix still reused the stale finding."""
    repo = _bug_repo(tmp_path)
    calls: list[str] = []
    _run_deep(repo, monkeypatch, calls)
    assert "leak-in-engine" in _xref_titles(repo)

    f = repo / "core" / "engine.py"
    f.write_text(f.read_text().replace("# BUG: leak-in-engine", "# fixed"))
    calls.clear()
    _run_deep(repo, monkeypatch, calls)             # same HEAD, dirty tree
    assert "leak-in-engine" not in _xref_titles(repo)
    assert calls.count("chunk") == 1                # only the edited chunk

    # …and the note written for the DIRTY content is reusable on the next
    # run over the same tree (hashed, not treated as always-dirty):
    calls.clear()
    _run_deep(repo, monkeypatch, calls)
    assert calls.count("chunk") == 0


def test_worktree_shas_track_edits_deletes_and_untracked(tmp_path):
    from luxe.gitkit import deep
    repo = _init_repo(tmp_path / "r", {"a.py": "a = 1\n", "b.py": "b = 1\n"})
    head = deep.git_file_shas(repo)
    (repo / "a.py").write_text("a = 2\n")
    (repo / "b.py").unlink()
    (repo / "c.py").write_text("c = 1\n")
    (repo / ".luxe").mkdir()
    (repo / ".luxe" / "memory.md").write_text("m\n")
    wt = deep.worktree_file_shas(repo)
    assert wt["a.py"] != head["a.py"]
    assert wt["a.py"] == _out(repo, "hash-object", "a.py")
    assert "b.py" not in wt
    assert wt["c.py"] == _out(repo, "hash-object", "c.py")
    assert not any(k.startswith(".luxe/") for k in wt)


def test_non_framing_index_js_edit_stays_incremental(tmp_path):
    """13: the framing trigger matched ANY index.js / app.py / main.* anywhere;
    only files the survey actually read (the saved framing list) count."""
    from luxe.gitkit import deep
    chunks = [deep.Chunk(index=i, files=[f"f{i}.py"], label="x",
                         est_tokens=100) for i in range(4)]
    pad = {f"f{i}.py": "s" for i in range(4)}
    pad.update({f"p{i}.py": "s" for i in range(20)})
    old = {**pad, "web/deep/index.js": "1", "README.md": "r"}
    new = {**old, "web/deep/index.js": "2"}
    plan = deep.plan_incremental(old_files=old, new_files=new, chunks=chunks,
                                 baseline=deep.make_baseline(chunks),
                                 added_recs=[], content_budget=1000,
                                 framing=["README.md"])
    assert plan.mode == "incremental"
    new2 = {**old, "README.md": "r2"}                # a file the survey read
    plan = deep.plan_incremental(old_files=old, new_files=new2, chunks=chunks,
                                 baseline=deep.make_baseline(chunks),
                                 added_recs=[], content_budget=1000,
                                 framing=["README.md"])
    assert plan.mode == "rebuild" and "README.md" in plan.reason


def test_incremental_path_does_not_walk_the_whole_tree(tmp_path, monkeypatch):
    """20: the incremental path enumerated (and line-counted) every file in the
    repo just to find the handful of added ones."""
    from luxe.gitkit import deep
    repo = _bug_repo(tmp_path)
    calls: list[str] = []
    _run_deep(repo, monkeypatch, calls)
    (repo / "core" / "newmod.py").write_text("n = 1\n" * 30)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add newmod")

    def boom(*a, **k):
        raise AssertionError("whole-tree enumerate_files on the incremental path")
    monkeypatch.setattr(deep, "enumerate_files", boom)
    calls.clear()
    out = _run_deep(repo, monkeypatch, calls)
    assert "incremental:" in out and calls.count("chunk") == 1


def test_symbols_for_is_built_once_per_partition():
    from luxe.gitkit import deep

    class _S:
        def __init__(self, path, name):
            self.path, self.name = path, name

    class _Idx:
        def __init__(self):
            self._symbols = [_S(f"f{i}.py", f"s{i}") for i in range(50)]
            self.reads = 0

        @property
        def symbols(self):
            self.reads += 1
            return self._symbols

    idx = _Idx()
    recs = [deep.FileRec(rel=f"f{i}.py", language="python", loc=1, bytes=4,
                         tokens=1, top_dir=".", priority=2) for i in range(50)]
    chunks = deep.build_chunks(recs, content_budget=1, symbol_index=idx)
    assert len(chunks) == 50
    assert idx.reads == 1                           # not once per chunk
    by_file = {c.files[0]: c.symbols for c in chunks}
    assert by_file["f3.py"] == ["s3"]


def test_unanalyzed_chunk_is_not_cached(tmp_path, monkeypatch):
    """16: an 'unanalyzed' (empty) chunk result was cached and reused, so a
    transient empty pass stuck as a permanent coverage gap."""
    from luxe.gitkit import deep
    repo = _bug_repo(tmp_path)
    calls: list[str] = []
    _run_deep(repo, monkeypatch, calls, stub=_bug_stub(repo, calls, empty=True))
    notes = list((deep._map_dir(repo) / "notes" / "gitaudit").glob("chunk-*.json"))
    assert notes == []
    calls.clear()
    _run_deep(repo, monkeypatch, calls)
    assert "leak-in-engine" in _xref_titles(repo)


def test_note_whose_recovery_pass_aborted_is_not_cached(tmp_path, monkeypatch):
    """16: a rambly chunk whose format-recovery pass ABORTED fell to heuristic
    salvage and was cached as if complete."""
    from luxe.gitkit import deep
    repo = _bug_repo(tmp_path)
    calls: list[str] = []
    ramble = ("let me look. I need to check. wait, okay, hmm actually, "
              "let me see\n" * 5
              + "1. **High** `core/engine.py:2` — leak in engine\n")

    def stub(backend, role_cfg, *, run_id="", extra_context="", **kw):
        stage = _stage(run_id)
        calls.append(stage)
        if stage == "survey":
            return _Res("Survey notes.")
        if stage == "synthesis":
            return _Res("# Repository audit\n**Findings: 1**\nok")
        if stage == "format":
            return _Res("", aborted=True)
        return _Res(ramble)
    _run_deep(repo, monkeypatch, calls, stub=stub)
    assert "format" in calls
    notes = list((deep._map_dir(repo) / "notes" / "gitaudit").glob("chunk-*.json"))
    assert notes == []


# --- digest cap, reduce, shared patterns (11, 12, 14) -----------------------

def _finding(i: int, sev: str) -> dict:
    return {"title": f"finding {i}", "root_cause": f"rc{i}", "severity": sev,
            "evidence": [f"m{i}.py:{i + 1}"], "chunk": 0}


def test_chunk_digest_block_is_capped_index_not_full_notes():
    """11: every chunk got json.dumps(digest) — markdown_notes included,
    unbounded."""
    from luxe.context import estimate_tokens
    from luxe.gitkit import deep
    d = deep.empty_digest()
    d["provisional_findings"] = [_finding(i, "high") for i in range(5)]
    body = "Long explanation paragraph that should not be copied. " * 40
    d["markdown_notes"] = [
        {"chunk": i, "label": "x", "source": "md_clean",
         "md": f"- **high** `n{i}.py:3` — note finding {i}\n\n{body}"}
        for i in range(30)]
    block = deep._digest_block(d, max_tokens=400)
    assert "Long explanation paragraph" not in block   # index only
    assert "finding 0" in block and "m0.py:1" in block
    assert estimate_tokens(block) < 600
    assert "more earlier findings not listed here" in block


def test_compact_digest_never_drops_high_and_logs_truthfully():
    """11: over the ceiling it dropped 'lowest severity first' straight
    through criticals, and logged every drop as 'low-severity'."""
    from luxe.gitkit import deep
    d = deep.empty_digest()
    d["provisional_findings"] = ([_finding(i, "critical") for i in range(5)]
                                 + [_finding(10 + i, "low") for i in range(3)])
    logs: list[str] = []
    out = deep.compact_digest(d, ceiling_tokens=10, log=logs.append)
    sevs = [f["severity"] for f in out["provisional_findings"]]
    assert sevs.count("critical") == 5 and "low" not in sevs
    assert any("3 low" in m for m in logs)
    assert any("still over budget" in m for m in logs)


def test_reduce_runs_on_synth_role_and_consolidates_notes():
    """12: the reduce ran on the chunk role (pass_fn default) and only touched
    provisional_findings, so an overflow made of markdown notes never
    shrank."""
    import json as _json
    from luxe.gitkit import deep
    d = deep.empty_digest()
    d["provisional_findings"] = [_finding(0, "high")]
    d["markdown_notes"] = [{"chunk": i, "label": "x", "source": "md_clean",
                            "md": f"- **medium** `n{i}.py:2` — thing {i}"}
                           for i in range(6)]
    roles: list = []

    def fake_pass(goal, ctx, label, role=None):
        roles.append(role)
        return _Res("```json\n" + _json.dumps(
            {"findings": [{"title": "merged", "severity": "medium",
                           "evidence": ["n1.py:2"]}]}) + "\n```")
    synth = object()
    out = deep._reduce_findings(d, eff_ctx=100, pass_fn=fake_pass, role=synth)
    assert roles and all(r is synth for r in roles)
    assert out["markdown_notes"] == []                  # notes were reduced too
    titles = {f["title"] for f in out["provisional_findings"]}
    assert "merged" in titles


def test_severity_line_needs_a_whole_word():
    """14: `(critical|high|medium|low)\\b` without a leading \\b read
    "flow" / "allow" / "below" as severity `low`."""
    from luxe.gitkit import deep
    assert deep._heuristic_findings("the data flow through `parse()` is odd") == []
    assert deep._heuristic_findings("see the list below in utils/x.py:12 please") == []
    assert deep._heuristic_findings("**low** — tidy `utils/x.py:12`")


def test_file_line_covers_more_languages():
    from luxe.gitkit import patterns
    for ref in ("src/App.java:12", "lib/x.rb:3", "a/b.kt line 9", "web/c.jsx:4"):
        assert patterns.FILE_LINE_RE.search(ref), ref
    assert patterns.FILE_LINE_RE.search("requests.get 5") is None


def test_render_report_does_not_double_count_structured_findings():
    """14: the count summed pf + heuristic lines over ALL sections, including
    the Additional-findings section that renders pf itself."""
    from luxe.gitkit import deep
    d = deep.empty_digest()
    d["provisional_findings"] = [_finding(i, "high") for i in range(3)]
    d["markdown_notes"] = [{"chunk": 0, "label": "x", "source": "md_clean",
                            "md": "- **medium** `n.py:2` — one note finding"}]
    out = deep._render_report(d, "gitaudit")
    assert "**Findings: 4 " in out
