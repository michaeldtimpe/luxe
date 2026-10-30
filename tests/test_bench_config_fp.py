"""Configuration fingerprint on bench state (harvest candidate 3)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from benchmarks.maintain_suite import run as R

ENV = {"LUXE_TIERED_COMPACT": "1", "PATH": "/bin"}


def _fx(fid: str):
    return SimpleNamespace(id=fid)


def _done(output: Path, fid: str, env: dict | None, variant=None) -> None:
    st = R.FixtureState(fixture_id=fid, status=R.FixtureStatus.DONE, luxe_run_id="r1")
    if env is not None:
        st.config_fp, st.config_fp_fields = R.config_fingerprint(variant, env)
    R.save_state(output, st, variant.variant_id if variant else "")


def test_fingerprint_is_deterministic_and_ignores_non_luxe_env():
    a = R.config_fingerprint(None, ENV)
    b = R.config_fingerprint(None, {**ENV, "PATH": "/usr/bin", "HOME": "/x"})
    assert a == b


def test_luxe_switch_changes_fingerprint_and_is_named():
    fp1, f1 = R.config_fingerprint(None, ENV)
    fp2, f2 = R.config_fingerprint(None, {**ENV, "LUXE_TIERED_COMPACT": "0"})
    assert fp1 != fp2
    assert R.fingerprint_diff(f1, f2) == ["env:LUXE_TIERED_COMPACT"]
    _, f3 = R.config_fingerprint(None, {**ENV, "LUXE_NEW": "1"})
    assert R.fingerprint_diff(f1, f3) == ["env:LUXE_NEW"]


def test_env_values_are_hashed_never_stored():
    secret = "sk-test-not-a-real-value-123456"
    _, fields = R.config_fingerprint(None, {"LUXE_SOME_TOKEN": secret})
    assert secret not in json.dumps(fields)


def test_state_round_trips_and_old_state_still_loads(tmp_path):
    st = R.FixtureState(fixture_id="f", config_fp="abc", config_fp_fields={"code": "x"})
    back = R.FixtureState.from_dict(json.loads(json.dumps(st.to_dict())))
    assert back.config_fp == "abc" and back.config_fp_fields == {"code": "x"}
    old = R.FixtureState.from_dict({"fixture_id": "f", "status": "done"})
    assert old.config_fp == "" and old.config_fp_fields == {}


def test_stale_done_cells(tmp_path):
    _done(tmp_path, "same", ENV)
    _done(tmp_path, "changed", {**ENV, "LUXE_TIERED_COMPACT": "0"})
    _done(tmp_path, "legacy", None)
    R.save_state(tmp_path, R.FixtureState(fixture_id="err", status=R.FixtureStatus.ERROR,
                                          config_fp="zzz"))
    plan = [(None, _fx(f)) for f in ("same", "changed", "legacy", "err", "never_ran")]
    stale, legacy = R.stale_done_cells(tmp_path, plan, environ=ENV)
    assert stale == ["changed: env:LUXE_TIERED_COMPACT"]
    assert legacy == 1
    assert not (tmp_path / "never_ran").exists(), "preflight must not create cell dirs"


def test_preflight_refuses_and_force_overrides(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(R.os, "environ", {**ENV, "LUXE_TIERED_COMPACT": "0"})
    _done(tmp_path, "f1", ENV)
    plan = [(None, _fx("f1"))]
    assert R._fingerprint_preflight(tmp_path, plan, SimpleNamespace(force=False)) is False
    out = capsys.readouterr().out
    assert "f1: env:LUXE_TIERED_COMPACT" in out and "--force" in out
    assert R._fingerprint_preflight(tmp_path, plan, SimpleNamespace(force=True)) is True
    # The refusal deleted nothing.
    assert (tmp_path / "f1" / "state.json").is_file()
