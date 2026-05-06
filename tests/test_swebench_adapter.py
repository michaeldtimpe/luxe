"""Tests for benchmarks/swebench/adapter.py — SpecDD .sdd injection helpers.

The end-to-end run_instance path requires git + a real luxe install +
oMLX backend; these tests cover only the deterministic glue:
write_swebench_sdd, remove_swebench_sdd, the synthetic-contract content
shape, and the paired-mechanism env wiring (write_pressure ↔ inject_sdd).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.swebench import adapter as adapter_mod
from benchmarks.swebench.adapter import (
    SWEBENCH_SDD_BODY,
    remove_swebench_sdd,
    run_instance,
    write_swebench_sdd,
)
from luxe.sdd import parse_sdd
from luxe.spec_resolver import _glob_matches


class TestSwebenchSddBody:
    def test_parses_cleanly(self):
        sf = parse_sdd(SWEBENCH_SDD_BODY)
        assert sf.title == "swebench-fixture"
        assert sf.forbids  # has at least one Forbids glob

    def test_blocks_observed_n75_leakage_paths(self):
        """The four literal paths the model created at n=75 must all
        match a Forbids glob. Without this guard, a broader/loose glob
        update could silently cease to fire."""
        sf = parse_sdd(SWEBENCH_SDD_BODY)
        leakage_paths = [
            "test_fix.py",                     # django-10097, sympy-13877
            "xarray/test_fix.py",              # xarray-3305
            "sympy/test_det_fix.py",           # sympy-13877 alternate
            "repo_root/test_encoded_file.py",  # pytest-5262
            "src/test_encoded_file.py",        # pytest-5262 alternate
        ]
        for path in leakage_paths:
            assert any(_glob_matches(g, path) for g in sf.forbids), (
                f"{path!r} not blocked by any Forbids glob; "
                f"globs={sf.forbids}"
            )

    def test_does_not_block_legitimate_test_paths(self):
        """Existing test files in standard layouts must NOT trip Forbids.
        The model needs to read these to understand existing test patterns.

        Boundary-case entries (test_runtime.py, test_data_verification.py)
        prove the v1.5 broad globs anchor on `_time.py` / `_verify.py`
        SUFFIXES, not the bare `time` / `verify` substrings.
        """
        sf = parse_sdd(SWEBENCH_SDD_BODY)
        legit_paths = [
            "tests/test_models.py",
            "tests/conftest.py",
            "tests/test_array.py",
            "src/django/tests/test_admin.py",
            "lib/matplotlib/tests/test_axes.py",
            "astropy/tests/test_units.py",
            "tests/test_runtime.py",            # substring 'time' but no _time.py suffix
            "tests/test_data_verification.py",  # substring 'verify' but no _verify.py suffix
        ]
        for path in legit_paths:
            assert not any(_glob_matches(g, path) for g in sf.forbids), (
                f"legitimate test file {path!r} would be blocked; "
                f"check Forbids globs are not over-broad: {sf.forbids}"
            )

    @pytest.mark.parametrize("path,instance", [
        ("verify_fix.py",                       "psf__requests-1921 / pydata__xarray-3677 / pydata__xarray-3151"),
        ("repo/verify_fix.py",                  "pytest-dev__pytest-10051"),
        ("xarray/tests/test_fix_verify.py",     "pydata__xarray-3151"),
        ("tmp_test.py",                         "pylint-dev__pylint-4970 (1/2)"),
        ("tmp_install.py",                      "pylint-dev__pylint-4970 (2/2)"),
        ("lib/matplotlib/test_verify.py",       "matplotlib__matplotlib-14623"),
        ("sklearn/test_refit_time.py",          "scikit-learn__scikit-learn-11310"),
        ("sympy/test_verify.py",                "sympy__sympy-12481"),
    ])
    def test_blocks_observed_v15_pressure_paths(self, path, instance):
        """v1.5 paired-mechanism rerun produced 8 new_file_in_diff cases —
        write_pressure actuation found names un-covered by the original
        Forbids list. Each escaped path here must match at least one
        Forbids glob; a regression points at the exact filename and the
        instance it came from.

        Source:
        acceptance/swebench/post_specdd_v15_pressure_n75/rep_1/
        """
        sf = parse_sdd(SWEBENCH_SDD_BODY)
        assert any(_glob_matches(g, path) for g in sf.forbids), (
            f"{path!r} (from {instance}) escaped the v1.5 Forbids tightening; "
            f"globs={sf.forbids}"
        )


class TestWriteRemoveSdd:
    def test_write_creates_file_named_after_repo(self, tmp_path: Path):
        repo = tmp_path / "myproject"
        repo.mkdir()
        sdd = write_swebench_sdd(repo)
        assert sdd == repo / "myproject.sdd"
        assert sdd.is_file()
        assert sdd.read_text(encoding="utf-8") == SWEBENCH_SDD_BODY

    def test_remove_deletes_file(self, tmp_path: Path):
        repo = tmp_path / "myproject"
        repo.mkdir()
        write_swebench_sdd(repo)
        remove_swebench_sdd(repo)
        assert not (repo / "myproject.sdd").exists()

    def test_remove_is_idempotent(self, tmp_path: Path):
        repo = tmp_path / "myproject"
        repo.mkdir()
        # Calling remove on a clean repo is a no-op.
        remove_swebench_sdd(repo)
        # Calling twice is also fine.
        write_swebench_sdd(repo)
        remove_swebench_sdd(repo)
        remove_swebench_sdd(repo)
        assert not (repo / "myproject.sdd").exists()

    def test_round_trip_via_find_all_sdd(self, tmp_path: Path):
        """The injected file is discoverable by find_all_sdd
        (canonical-placement check). This is what the prompt-side
        block builder uses to surface contracts to the model."""
        from luxe.spec_resolver import find_all_sdd

        repo = tmp_path / "django"
        repo.mkdir()
        write_swebench_sdd(repo)
        sdds = find_all_sdd(repo)
        assert len(sdds) == 1
        assert sdds[0].title == "swebench-fixture"


class TestPairedMechanismEnv:
    """Verify run_instance wires LUXE_WRITE_PRESSURE alongside inject_sdd.

    Constraint (.sdd) and actuation (write_pressure) ship together:
    n=75 measured `empty_patch +4` when constraint shipped without
    actuation, so the adapter binds them by default and lets ablation
    flip them off via the explicit kwargs.
    """

    @pytest.fixture
    def captured_env(self, tmp_path: Path, monkeypatch):
        """Replace ensure_repo + invoke_luxe_maintain with stubs that capture env."""
        captured = {}

        def fake_ensure_repo(instance, work_dir):
            r = work_dir / instance.instance_id
            r.mkdir(parents=True, exist_ok=True)
            return r

        def fake_invoke(instance, repo, log_dir, *, config=None, extra_env=None, timeout_s=None):
            captured["extra_env"] = dict(extra_env) if extra_env else {}
            return 0, "", ""

        def fake_extract_diff(repo, base_commit):
            return ""

        monkeypatch.setattr(adapter_mod, "ensure_repo", fake_ensure_repo)
        monkeypatch.setattr(adapter_mod, "invoke_luxe_maintain", fake_invoke)
        monkeypatch.setattr(adapter_mod, "extract_diff", fake_extract_diff)
        return captured

    def _instance(self):
        from benchmarks.swebench.fixtures import SweBenchInstance
        return SweBenchInstance(
            instance_id="paired__test_1",
            repo="paired/test",
            base_commit="0" * 40,
            problem_statement="trivial",
        )

    def test_default_pairs_inject_sdd_with_write_pressure(self, tmp_path, captured_env):
        # Default kwargs: inject_sdd=True, write_pressure=True → env carries pressure flag.
        run_instance(self._instance(), tmp_path)
        assert captured_env["extra_env"].get("LUXE_WRITE_PRESSURE") == "1"

    def test_no_inject_sdd_skips_write_pressure(self, tmp_path, captured_env):
        # Pre-Lever-2 baseline: no .sdd, no actuation. The pair stays paired.
        run_instance(self._instance(), tmp_path, inject_sdd=False)
        assert "LUXE_WRITE_PRESSURE" not in captured_env["extra_env"]

    def test_explicit_write_pressure_false_skips_pressure(self, tmp_path, captured_env):
        # Ablation: .sdd injected but pressure disabled — measure constraint in isolation.
        run_instance(self._instance(), tmp_path, inject_sdd=True, write_pressure=False)
        assert "LUXE_WRITE_PRESSURE" not in captured_env["extra_env"]

    def test_extra_env_preserved_alongside_pressure(self, tmp_path, captured_env):
        # Caller-supplied extra_env must merge with, not be replaced by, the pressure flag.
        run_instance(self._instance(), tmp_path, extra_env={"FOO": "bar"})
        env = captured_env["extra_env"]
        assert env.get("FOO") == "bar"
        assert env.get("LUXE_WRITE_PRESSURE") == "1"