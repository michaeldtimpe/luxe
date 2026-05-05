"""Tests for benchmarks/swebench/adapter.py — SpecDD .sdd injection helpers.

The end-to-end run_instance path requires git + a real luxe install +
oMLX backend; these tests cover only the deterministic glue:
write_swebench_sdd, remove_swebench_sdd, and the synthetic-contract
content shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.swebench.adapter import (
    SWEBENCH_SDD_BODY,
    remove_swebench_sdd,
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
        """
        sf = parse_sdd(SWEBENCH_SDD_BODY)
        legit_paths = [
            "tests/test_models.py",
            "tests/conftest.py",
            "src/django/tests/test_admin.py",
            "lib/matplotlib/tests/test_axes.py",
        ]
        for path in legit_paths:
            assert not any(_glob_matches(g, path) for g in sf.forbids), (
                f"legitimate test file {path!r} would be blocked; "
                f"check Forbids globs are not over-broad: {sf.forbids}"
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