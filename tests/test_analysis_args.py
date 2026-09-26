"""Analysis-tool argument hygiene and result parsing (2026-09-26).

- `path` is model-supplied and went into argv raw: `lint(path="--fix")` ran
  `ruff check --fix`, i.e. a READ-ONLY tool rewrote files. Now it is refused
  when it starts with '-', validated through `fs._resolve_rel`, and placed
  behind `--`.
- `security_scan` parsed bandit's whole report dict as "findings" (count = 4,
  the number of top-level keys, plus an uncapped per-file metrics blob).
- `deps_audit` audited whatever environment pip-audit happened to run in
  (uvx's throwaway env, or luxe's own venv) — never the repo.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from luxe.tools import analysis, fs


@pytest.fixture
def repo(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("import os\n")
    (tmp_path / "repo2").mkdir()
    fs.set_repo_root(root)
    yield root
    fs._REPO_ROOT = None


class _Spy:
    def __init__(self, stdout: str = "[]", returncode: int = 0, write=None):
        self.cmds: list[list[str]] = []
        self.stdout, self.returncode, self.write = stdout, returncode, write

    def __call__(self, cmd, **kw):
        self.cmds.append(list(cmd))
        if self.write is not None:
            self.write(cmd)
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


@pytest.fixture
def spy(monkeypatch):
    s = _Spy()
    monkeypatch.setattr(analysis.subprocess, "run", s)
    monkeypatch.setattr(analysis, "_resolve", lambda tool, **kw: [tool])
    return s


class TestPathArgument:
    @pytest.mark.parametrize("fn", ["lint", "typecheck", "security_scan", "lint_js"])
    def test_option_shaped_path_refused_without_spawning(self, repo, spy, fn):
        out, err = analysis._ANALYZERS[fn]["fn"]({"path": "--fix"})
        assert out == "" and err and "-" in err
        assert spy.cmds == []

    @pytest.mark.parametrize("fn", ["lint", "typecheck", "security_scan"])
    def test_escaping_path_refused_without_spawning(self, repo, spy, fn):
        out, err = analysis._ANALYZERS[fn]["fn"]({"path": "../repo2"})
        assert out == "" and err and "escapes repo root" in err
        assert spy.cmds == []

    @pytest.mark.parametrize("fn", ["lint", "typecheck", "security_scan"])
    def test_path_goes_behind_double_dash(self, repo, spy, fn):
        analysis._ANALYZERS[fn]["fn"]({"path": "a.py"})
        cmd = spy.cmds[0]
        assert cmd[-2:] == ["--", "a.py"]

    def test_default_path_is_repo_root(self, repo, spy):
        analysis._lint({})
        assert spy.cmds[0][-2:] == ["--", "."]


def _bandit_report(n: int) -> dict:
    return {
        "errors": [],
        "generated_at": "x",
        "metrics": {"./a.py": {"loc": 1}, "_totals": {"loc": 1}},
        "results": [{"test_id": "B404", "filename": "./a.py", "line_number": i,
                     "issue_severity": "LOW", "issue_text": "t"} for i in range(n)],
    }


class TestSecurityScanParsing:
    def _writer(self, report: dict):
        def write(cmd):
            out = cmd[cmd.index("-o") + 1]
            Path(out).write_text(json.dumps(report))
        return write

    def test_findings_are_the_results_list_not_the_report_keys(self, repo, monkeypatch):
        s = _Spy(stdout="Working... ━━━━ 100%\n", returncode=1,
                 write=self._writer(_bandit_report(3)))
        monkeypatch.setattr(analysis.subprocess, "run", s)
        monkeypatch.setattr(analysis, "_resolve", lambda tool, **kw: [tool])
        out, err = analysis._security_scan({"path": "."})
        assert err is None
        payload = json.loads(out)
        assert payload["status"] == "ok"
        assert payload["count"] == 3
        assert [f["test_id"] for f in payload["findings"]] == ["B404"] * 3
        assert "metrics" not in out
        assert "-q" in s.cmds[0]

    def test_findings_are_capped_and_the_cap_announced(self, repo, monkeypatch):
        s = _Spy(returncode=1, write=self._writer(_bandit_report(200)))
        monkeypatch.setattr(analysis.subprocess, "run", s)
        monkeypatch.setattr(analysis, "_resolve", lambda tool, **kw: [tool])
        payload = json.loads(analysis._security_scan({"path": "."})[0])
        assert payload["count"] == 150
        assert payload["truncated"] is True and payload["total"] == 200

    def test_no_report_and_nonzero_exit_is_an_error_not_a_pass(self, repo, monkeypatch):
        s = _Spy(stdout="", returncode=2)
        monkeypatch.setattr(analysis.subprocess, "run", s)
        monkeypatch.setattr(analysis, "_resolve", lambda tool, **kw: [tool])
        payload = json.loads(analysis._security_scan({"path": "."})[0])
        assert payload["status"] == "error"

    @pytest.mark.skipif(analysis._resolve("bandit", module="bandit") is None,
                        reason="bandit not installed")
    def test_real_bandit_reports_its_findings(self, repo):
        (repo / "a.py").write_text(
            "import subprocess\nsubprocess.call('ls', shell=True)\n")
        payload = json.loads(analysis._security_scan({"path": "."})[0])
        assert payload["status"] == "ok"
        assert payload["count"] >= 1
        assert all("test_id" in f for f in payload["findings"])


_PIP_AUDIT = {
    "dependencies": [
        {"name": "requests", "version": "2.19.0", "vulns": [
            {"id": "PYSEC-2018-28", "fix_versions": ["2.20.0"],
             "aliases": ["CVE-2018-18074"]}]},
        {"name": "idna", "version": "3.7", "vulns": []},
        {"name": "localpkg", "skip_reason": "not on PyPI"},
    ],
    "fixes": [],
}


class TestDepsAuditTargetsTheRepo:
    def test_requirements_files_are_audited(self, repo, monkeypatch):
        (repo / "requirements.txt").write_text("requests==2.19.0\n")
        (repo / "requirements-dev.txt").write_text("pytest\n")
        s = _Spy(stdout=json.dumps(_PIP_AUDIT), returncode=1)
        monkeypatch.setattr(analysis.subprocess, "run", s)
        monkeypatch.setattr(analysis, "_resolve", lambda tool, **kw: [tool])
        out, err = analysis._deps_audit({})
        cmd = s.cmds[0]
        assert ["-r", "requirements-dev.txt"] == cmd[cmd.index("requirements-dev.txt") - 1:][:2]
        assert "requirements.txt" in cmd
        payload = json.loads(out)
        assert payload["status"] == "ok"
        assert payload["count"] == 1
        assert payload["findings"][0] == {
            "package": "requests", "version": "2.19.0", "id": "PYSEC-2018-28",
            "aliases": ["CVE-2018-18074"], "fix_versions": ["2.20.0"]}

    def test_project_dir_is_audited_when_no_requirements(self, repo, monkeypatch):
        (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
        s = _Spy(stdout=json.dumps({"dependencies": [], "fixes": []}))
        monkeypatch.setattr(analysis.subprocess, "run", s)
        monkeypatch.setattr(analysis, "_resolve", lambda tool, **kw: [tool])
        payload = json.loads(analysis._deps_audit({})[0])
        assert s.cmds[0][-1] == "."
        assert payload == {"status": "ok", "findings": [], "count": 0}

    def test_nothing_to_audit_is_skipped_not_run(self, repo, spy):
        payload = json.loads(analysis._deps_audit({})[0])
        assert payload["status"] == "skipped"
        assert spy.cmds == []
