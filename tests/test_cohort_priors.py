"""v1.11 Phase 2 — cohort_priors loader tests.

Null-safe + schema-validation coverage. The loader must never raise on
malformed input.
"""
from __future__ import annotations

import json
from pathlib import Path

from luxe.agents.cohort_priors import (
    _VALID_VERDICTS,
    load_prior,
    load_prior_from_env,
)


def _write(tmp_path: Path, name: str, data: dict | str) -> Path:
    p = tmp_path / f"{name}.json"
    if isinstance(data, str):
        p.write_text(data)
    else:
        p.write_text(json.dumps(data))
    return p


def test_load_prior_returns_none_for_empty_instance_id(tmp_path: Path):
    assert load_prior("", base_dir=tmp_path) is None


def test_load_prior_returns_none_when_file_missing(tmp_path: Path):
    assert load_prior("instance__does_not_exist", base_dir=tmp_path) is None


def test_load_prior_returns_none_when_base_dir_missing(tmp_path: Path):
    nonexistent = tmp_path / "nonexistent"
    assert load_prior("any", base_dir=nonexistent) is None


def test_load_prior_returns_none_on_malformed_json(tmp_path: Path):
    _write(tmp_path, "broken_id", "not valid json {")
    assert load_prior("broken_id", base_dir=tmp_path) is None


def test_load_prior_returns_none_on_non_dict_root(tmp_path: Path):
    _write(tmp_path, "list_root", ["not", "a", "dict"])  # type: ignore[arg-type]
    assert load_prior("list_root", base_dir=tmp_path) is None


def test_load_prior_returns_none_on_missing_instance_id_field(tmp_path: Path):
    _write(tmp_path, "no_id", {"verdict": "byte_identical"})
    assert load_prior("no_id", base_dir=tmp_path) is None


def test_load_prior_returns_none_on_missing_verdict_field(tmp_path: Path):
    _write(tmp_path, "no_verdict", {"instance_id": "no_verdict"})
    assert load_prior("no_verdict", base_dir=tmp_path) is None


def test_load_prior_returns_none_on_invalid_verdict(tmp_path: Path):
    _write(tmp_path, "bad_verdict", {
        "instance_id": "bad_verdict", "verdict": "not_a_real_verdict",
    })
    assert load_prior("bad_verdict", base_dir=tmp_path) is None


def test_load_prior_returns_none_when_filename_id_mismatches_content(tmp_path: Path):
    _write(tmp_path, "filename_id", {
        "instance_id": "different_id", "verdict": "byte_identical",
    })
    assert load_prior("filename_id", base_dir=tmp_path) is None


def test_load_prior_returns_dict_on_valid_input(tmp_path: Path):
    data = {
        "instance_id": "ok",
        "verdict": "deterministic_gain",
        "label_a": "v1.10.4",
        "label_b": "v1.10.5",
        "tiers_a": ["empty_patch"] * 3,
        "tiers_b": ["wrong_location"] * 3,
        "median_rank_a": 6.0,
        "median_rank_b": 3.0,
        "rank_delta": -3.0,
    }
    _write(tmp_path, "ok", data)
    loaded = load_prior("ok", base_dir=tmp_path)
    assert loaded is not None
    assert loaded["instance_id"] == "ok"
    assert loaded["verdict"] == "deterministic_gain"
    assert loaded["rank_delta"] == -3.0


def test_load_prior_preserves_extra_fields(tmp_path: Path):
    """Permissive: unknown fields are kept (no schema-strict rejection)."""
    data = {
        "instance_id": "extras", "verdict": "byte_identical",
        "future_field": "v1.12 will use this",
    }
    _write(tmp_path, "extras", data)
    loaded = load_prior("extras", base_dir=tmp_path)
    assert loaded is not None
    assert loaded["future_field"] == "v1.12 will use this"


def test_all_valid_verdicts_load(tmp_path: Path):
    for v in _VALID_VERDICTS:
        _write(tmp_path, v, {"instance_id": v, "verdict": v})
        assert load_prior(v, base_dir=tmp_path) is not None


def test_load_prior_from_env_returns_none_when_priors_flag_off(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("LUXE_LOAD_PRIORS", raising=False)
    monkeypatch.setenv("LUXE_INSTANCE_ID", "any")
    assert load_prior_from_env() is None


def test_load_prior_from_env_returns_none_when_instance_id_unset(monkeypatch):
    monkeypatch.setenv("LUXE_LOAD_PRIORS", "1")
    monkeypatch.delenv("LUXE_INSTANCE_ID", raising=False)
    assert load_prior_from_env() is None


def test_load_prior_from_env_returns_none_when_file_missing(monkeypatch):
    """Even when both env vars are set, missing file returns None."""
    monkeypatch.setenv("LUXE_LOAD_PRIORS", "1")
    monkeypatch.setenv("LUXE_INSTANCE_ID", "instance__never_existed_xyz")
    assert load_prior_from_env() is None
