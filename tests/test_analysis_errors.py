"""A crashed linter reported "ok" with zero findings.

`_run_tool` never consulted `returncode`. A ruff that fell over on a broken
config, a mypy that could not parse the repo, an eslint with a missing plugin —
each came back as `{"status": "ok", "findings": [], "count": 0}`, which reads
as *lint passed*. That is the exact false signal `_skipped` was written to
prevent for a MISSING binary; a binary that is present and fails produced it
anyway.

Two neighbours, same module, same shape of lie:

- the `_MAX_FINDINGS` (150) cut was silent, so 150 findings and 1,286 findings
  were the same JSON — `count` read as authoritative;
- the spawn inherited stdin and decoded strictly (see `test_bash_stdin.py`).

A non-zero exit WITH findings stays `ok`: that is what mypy does when it finds
type errors, and it is not a failure.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from luxe.tools import analysis, fs


@pytest.fixture(autouse=True)
def set_root(tmp_repo: Path):
    fs.set_repo_root(tmp_repo)
    yield
    fs._REPO_ROOT = None


def _fake(monkeypatch, *, rc: int, stdout: str = "", stderr: str = "") -> dict:
    """Replace the analyzer spawn with a fixed result; return the seen kwargs."""
    seen: dict = {}

    def _run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    monkeypatch.setattr(analysis.subprocess, "run", _run)
    return seen


def _payload(monkeypatch, *, rc: int, stdout: str = "", stderr: str = "",
             parse_json: bool = True) -> dict:
    _fake(monkeypatch, rc=rc, stdout=stdout, stderr=stderr)
    out, err = analysis._run_tool(["ruff", "check"], parse_json=parse_json)
    assert err is None, err
    return json.loads(out)


class TestACrashIsNotACleanBillOfHealth:
    def test_json_tool_that_exits_nonzero_without_json(self, monkeypatch):
        got = _payload(monkeypatch, rc=2,
                       stderr="ruff failed: Failed to parse pyproject.toml")
        assert got["status"] == "error"
        assert got["exit_code"] == 2
        assert "pyproject.toml" in got["stderr"]

    def test_the_error_says_the_check_did_not_pass(self, monkeypatch):
        """Naming the failure without saying what it means is how this got
        read as "lint passed" in the first place."""
        got = _payload(monkeypatch, rc=2, stderr="boom")
        assert "did NOT pass" in got["reason"]

    def test_nonzero_with_no_output_at_all(self, monkeypatch):
        got = _payload(monkeypatch, rc=1, parse_json=False)
        assert got["status"] == "error"
        assert got["exit_code"] == 1

    def test_nonzero_with_an_empty_json_findings_list(self, monkeypatch):
        got = _payload(monkeypatch, rc=1, stdout="[]")
        assert got["status"] == "error"

    def test_there_is_no_findings_key_to_misread(self, monkeypatch):
        got = _payload(monkeypatch, rc=2, stderr="boom")
        assert "findings" not in got and "count" not in got


class TestOrdinaryFailuresStayOk:
    def test_nonzero_with_parsed_findings_is_normal_linter_behaviour(self, monkeypatch):
        got = _payload(monkeypatch, rc=1,
                       stdout=json.dumps([{"code": "F401", "message": "unused"}]))
        assert got["status"] == "ok"
        assert got["count"] == 1

    def test_nonzero_with_text_findings_is_normal_too(self, monkeypatch):
        """mypy exits 1 when it reports type errors."""
        got = _payload(monkeypatch, rc=1, stdout="a.py:1: error: bad\n",
                       parse_json=False)
        assert got["status"] == "ok"
        assert got["findings"] == ["a.py:1: error: bad"]

    def test_a_clean_run_is_byte_identical(self, monkeypatch):
        _fake(monkeypatch, rc=0, stdout="[]")
        out, err = analysis._run_tool(["ruff", "check"], parse_json=True)
        assert err is None
        assert out == json.dumps({"status": "ok", "findings": [], "count": 0},
                                 indent=2)

    def test_a_clean_text_run_is_byte_identical(self, monkeypatch):
        _fake(monkeypatch, rc=0, stdout="")
        out, _ = analysis._run_tool(["mypy", "."], parse_json=False)
        assert out == json.dumps({"status": "ok", "findings": [], "count": 0},
                                 indent=2)

    def test_unparseable_json_on_a_zero_exit_still_falls_back_to_lines(self,
                                                                      monkeypatch):
        got = _payload(monkeypatch, rc=0, stdout="not json at all")
        assert got["status"] == "ok"
        assert got["findings"] == ["not json at all"]


class TestTheFindingCapIsAnnounced:
    def test_json_findings_over_the_cap(self, monkeypatch):
        many = [{"code": f"E{i}"} for i in range(400)]
        got = _payload(monkeypatch, rc=1, stdout=json.dumps(many))
        assert got["count"] == analysis._MAX_FINDINGS
        assert got["truncated"] is True
        assert got["total"] == 400

    def test_text_findings_over_the_cap(self, monkeypatch):
        lines = "".join(f"a.py:{i}: error: bad\n" for i in range(400))
        got = _payload(monkeypatch, rc=1, stdout=lines, parse_json=False)
        assert got["truncated"] is True
        assert got["total"] == 400

    def test_under_the_cap_carries_no_truncation_keys(self, monkeypatch):
        got = _payload(monkeypatch, rc=1,
                       stdout=json.dumps([{"code": "F401"}]))
        assert "truncated" not in got and "total" not in got


class TestTheSpawn:
    def test_stdin_is_devnull_and_decoding_replaces(self, monkeypatch):
        seen = _fake(monkeypatch, rc=0, stdout="[]")
        analysis._run_tool(["ruff", "check"], parse_json=True)
        assert seen.get("stdin") is subprocess.DEVNULL
        assert seen.get("errors") == "replace"

    def test_a_missing_binary_still_degrades_to_skipped(self, monkeypatch):
        def _boom(cmd, **kw):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr(analysis.subprocess, "run", _boom)
        out, err = analysis._run_tool(["ruff", "check"])
        assert err is None
        assert json.loads(out)["status"] == "skipped"

    def test_a_timeout_still_returns_a_tool_error(self, monkeypatch):
        def _boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

        monkeypatch.setattr(analysis.subprocess, "run", _boom)
        out, err = analysis._run_tool(["ruff", "check"])
        assert out == ""
        assert err and "timed out" in err
