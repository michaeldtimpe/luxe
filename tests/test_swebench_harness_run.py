"""SWE-bench denominators and infra handling (no Docker, no network).

- harness.collect_results built its denominator from the harness's log
  dirs; the harness skips empty patches, so they vanished and the
  resolution rate inflated.
- run.py cached setup_failed (clone/fetch failure) as an empty patch
  forever and predicted it unresolved.
- --subset silently dropped ids absent from the dataset dump.
- clone/fetch had no wall bound.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from benchmarks.swebench import adapter as adapter_mod
from benchmarks.swebench import harness
from benchmarks.swebench import run as swe_run
from benchmarks.swebench.adapter import SweBenchInvocationResult
from benchmarks.swebench.fixtures import SweBenchInstance


def _inst(iid: str) -> SweBenchInstance:
    return SweBenchInstance(instance_id=iid, repo="o/r", base_commit="0" * 40,
                            problem_statement="p")


def test_denominator_counts_every_prediction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rep = tmp_path / "logs" / "run_evaluation" / "r1" / "model" / "a__b-1"
    rep.mkdir(parents=True)
    (rep / "report.json").write_text(json.dumps({"a__b-1": {"resolved": True}}))
    preds = {
        "a__b-1": {"instance_id": "a__b-1", "model_patch": "diff --git a b"},
        "a__b-2": {"instance_id": "a__b-2", "model_patch": ""},
        "a__b-3": {"instance_id": "a__b-3", "model_patch": "diff --git c d"},
    }
    res = harness.collect_results("r1", tmp_path / "out", predictions=preds)
    assert set(res) == set(preds)
    assert res["a__b-2"].raw.get("empty_patch") and not res["a__b-2"].resolved
    assert res["a__b-3"].error == "not_evaluated"
    harness.write_harness_summary(res, tmp_path / "hs.json")
    s = json.loads((tmp_path / "hs.json").read_text())
    assert s["n"] == 3 and s["n_resolved"] == 1
    assert s["resolution_rate"] == pytest.approx(1 / 3)


def test_subset_with_unknown_ids_is_an_error():
    with pytest.raises(ValueError, match="not in the dataset"):
        swe_run._filter_to_subset([_inst("a__b-1")], ["a__b-1", "zz__missing-9"])


def test_clone_and_fetch_are_wall_bounded(tmp_path, monkeypatch):
    seen = []

    def fake_run(cmd, cwd=None, env=None, timeout_s=None):
        seen.append((cmd[1], timeout_s))
        if cmd[1] == "clone":
            (Path(cmd[-1]) / ".git").mkdir(parents=True)
        return 0, "", ""
    monkeypatch.setattr(adapter_mod, "_run", fake_run)
    adapter_mod.ensure_repo(_inst("a__b-1"), tmp_path)
    by = dict(seen)
    assert by["clone"] and by["fetch"]


def test_setup_failed_is_not_cached_or_predicted(tmp_path, monkeypatch):
    ds = tmp_path / "verified.jsonl"
    ds.write_text("{}\n")
    out = tmp_path / "out"
    monkeypatch.setattr(swe_run, "_preflight_check_venv_pollution", lambda: 0)
    monkeypatch.setattr(swe_run, "load_instances_from_json",
                        lambda p: [_inst("a__b-1"), _inst("a__b-2")])

    def fake_run_instance(instance, work_dir, **kw):
        if instance.instance_id == "a__b-1":
            return SweBenchInvocationResult(instance_id="a__b-1", wall_s=1.0,
                                            error="setup_failed: clone failed")
        return SweBenchInvocationResult(instance_id="a__b-2", wall_s=1.0,
                                        model_patch="diff --git x y\n")
    monkeypatch.setattr(swe_run, "run_instance", fake_run_instance)
    monkeypatch.setattr(sys, "argv", ["run", "--dataset", str(ds), "--output", str(out),
                                      "--work-dir", str(tmp_path / "wd")])
    assert swe_run.main() == 1
    preds = json.loads((out / "predictions.json").read_text())
    assert [p["instance_id"] for p in preds] == ["a__b-2"]
    assert "model_patch" not in json.loads((out / "a__b-1.json").read_text())

    # a legacy cache of a setup failure (empty patch + setup_failed) re-runs
    (out / "a__b-1.json").write_text(json.dumps(
        {"instance_id": "a__b-1", "model_patch": "", "error": "setup_failed: x"}))
    ran = []
    monkeypatch.setattr(swe_run, "run_instance",
                        lambda inst, wd, **kw: ran.append(inst.instance_id) or
                        SweBenchInvocationResult(instance_id=inst.instance_id,
                                                 model_patch="diff\n"))
    swe_run.main()
    assert ran == ["a__b-1"]
