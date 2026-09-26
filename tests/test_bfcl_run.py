"""benchmarks/bfcl/run.py — infra errors are neither graded nor cached, and a
missing answer file is fatal. No model, no network: every seam is stubbed."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import benchmarks.bfcl.run as brun
from benchmarks.bfcl.adapter import BfclInvocationResult


class _NoBackend:
    def __init__(self, *a, **k):
        pass


@pytest.fixture
def stub(monkeypatch, tmp_path):
    monkeypatch.setattr(brun, "Backend", _NoBackend)
    problems = [{"id": "irrelevance_0", "question": [[{"role": "user", "content": "hi"}]],
                 "function": []}]
    monkeypatch.setattr(brun, "load_problems", lambda cat, limit=None: problems)
    monkeypatch.setattr(brun, "load_ground_truth", lambda cat: {})

    def _argv(*cats):
        monkeypatch.setattr(sys, "argv", ["bfcl", "--categories", *cats,
                                          "--output", str(tmp_path / "out")])
    return _argv, tmp_path / "out"


def test_backend_exception_is_infra_error_not_irrelevance_pass(stub, monkeypatch):
    argv, out = stub
    argv("irrelevance")
    monkeypatch.setattr(brun, "run_problem_raw", lambda *a, **k: BfclInvocationResult(
        problem_id="irrelevance_0", actual_calls=[], wall_s=0.1,
        error="ConnectError: connection refused"))
    rc = brun.main()
    summary = json.loads((out / "summary.json").read_text())
    assert summary["totals"]["passed"] == 0
    assert summary["totals"]["infra_errors"] == 1
    assert summary["totals"]["n"] == 0            # excluded from the denominator
    assert not (out / "irrelevance" / "irrelevance_0.json").exists()  # not cached
    assert rc != 0


def test_poisoned_legacy_cache_record_is_rerun(stub, monkeypatch):
    argv, out = stub
    argv("irrelevance")
    cat = out / "irrelevance"
    cat.mkdir(parents=True)
    (cat / "irrelevance_0.json").write_text(json.dumps(
        {"id": "irrelevance_0", "passed": True, "error": "ReadTimeout: x"}))
    ran = []

    def _raw(*a, **k):
        ran.append(1)
        return BfclInvocationResult(problem_id="irrelevance_0", actual_calls=[],
                                    wall_s=0.1)
    monkeypatch.setattr(brun, "run_problem_raw", _raw)
    assert brun.main() == 0
    assert ran == [1]
    assert json.loads((cat / "irrelevance_0.json").read_text())["error"] == ""


def test_missing_ground_truth_file_is_fatal(stub, monkeypatch):
    argv, _out = stub
    argv("multi_turn_base")

    def _missing(cat):
        raise FileNotFoundError("BFCL ground truth not found")
    monkeypatch.setattr(brun, "load_ground_truth", _missing)
    monkeypatch.setattr(brun, "run_problem_multi_turn",
                        lambda *a, **k: pytest.fail("ran without ground truth"))
    assert brun.main() == 2


def test_load_ground_truth_raises_on_missing_file(tmp_path, monkeypatch):
    from benchmarks.bfcl import adapter
    monkeypatch.setenv("LUXE_BFCL_DATA_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        adapter.load_ground_truth("multi_turn_base")
    assert adapter.load_ground_truth("irrelevance") == {}
