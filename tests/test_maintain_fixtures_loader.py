"""benchmarks/maintain_suite/fixtures.py — the one fixtures.yaml loader.

fixtures.yaml pins `/Users/mtimpe/.luxe/fixture-cache/...`; on m1 the user is
`michaeltimpe`, so every reader except opencode_harness used a path that does
not exist there (fresh clones failed, origin pruning silently skipped,
regrade_local crashed). And m1's cache is read-only, so every bench push
failed — an environment fault that surfaced only as a score.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.maintain_suite import fixtures as fx_mod
import benchmarks.maintain_suite.run as br

ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _yaml(tmp_path: Path, url: str) -> Path:
    p = tmp_path / "fixtures.yaml"
    p.write_text(
        "fixtures:\n"
        "  - id: fx\n"
        f"    repo_url: {url}\n"
        "    goal: g\n"
        "    task_type: implement\n"
        "    expected_outcome: {kind: regex_present, pattern: x}\n")
    return p


def test_load_fixtures_remaps_another_hosts_cache_path(tmp_path):
    home = tmp_path / "home"
    (home / ".luxe" / "fixture-cache" / "neon-rain").mkdir(parents=True)
    p = _yaml(tmp_path, "/Users/somebody-else/.luxe/fixture-cache/neon-rain")
    [f] = fx_mod.load_fixtures(p, home=home)
    assert f.repo_url == str(home / ".luxe" / "fixture-cache" / "neon-rain")


def test_run_py_loads_through_the_shared_loader(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".luxe" / "fixture-cache" / "neon-rain").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    p = _yaml(tmp_path, "/Users/somebody-else/.luxe/fixture-cache/neon-rain")
    [f] = br._load_fixtures(p)
    assert f.repo_url == str(home / ".luxe" / "fixture-cache" / "neon-rain")


def test_regrade_local_loads_through_the_shared_loader(tmp_path, monkeypatch):
    rl = _load_script("regrade_local")
    seen = {}
    monkeypatch.setattr(rl, "_load_fixtures",
                        lambda: seen.setdefault("called", []) or [])
    rl.load_fixtures()
    assert "called" in seen


def _git_origin(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    return path


def _fixture(url: str):
    from benchmarks.maintain_suite.grade import Fixture
    return Fixture(id="fx", goal="g", task_type="implement",
                   expected_outcome={"kind": "regex_present"}, repo_url=url)


def test_origin_problem_ok_for_writable_repo(tmp_path):
    origin = _git_origin(tmp_path / "origin")
    assert fx_mod.origin_problem(_fixture(str(origin))) == ""


def test_origin_problem_flags_missing_origin(tmp_path):
    msg = fx_mod.origin_problem(_fixture(str(tmp_path / "nope")))
    assert "does not exist" in msg


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_origin_problem_flags_read_only_origin(tmp_path):
    origin = _git_origin(tmp_path / "origin")
    refs = origin / ".git" / "refs" / "heads"
    refs.chmod(0o555)
    try:
        msg = fx_mod.origin_problem(_fixture(str(origin)))
    finally:
        refs.chmod(0o755)
    assert "read-only" in msg and "chmod" in msg


def test_origin_problem_ignores_remote_urls():
    assert fx_mod.origin_problem(_fixture("https://github.com/a/b")) == ""
    assert fx_mod.origin_problem(_fixture("git@github.com:a/b.git")) == ""


def test_bench_refuses_to_start_on_origin_problem(tmp_path, monkeypatch, capsys):
    p = _yaml(tmp_path, str(tmp_path / "missing-origin"))
    monkeypatch.setattr(sys, "argv", ["run", "--fixtures", str(p), "--all",
                                      "--output", str(tmp_path / "acc"),
                                      "--work-dir", str(tmp_path / "wd")])
    monkeypatch.setattr(br, "run_fixture", lambda *a, **k: pytest.fail("ran"))
    assert br.main() == 2
    assert "origin preflight failed" in capsys.readouterr().out
