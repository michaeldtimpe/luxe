"""Offline tests for CodeNeedle adapter, grader, and the vendored scorer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.codeneedle.adapter import build_prompt
from benchmarks.codeneedle.grade import aggregate_items
from benchmarks.codeneedle.upstream.scorer import score as score_function


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "benchmarks/codeneedle/manifest.json"


class TestBuildPrompt:
    def test_py_prompt_has_signature_marker(self):
        out = build_prompt(
            file_contents="def foo():\n    pass\n",
            function_name="foo",
            language="py",
            n_lines=10,
        )
        assert "def foo(" in out
        assert "reproduce verbatim the first 10 lines" in out
        assert "/no_think" in out  # default suppress_thinking=True

    def test_js_prompt_has_brace_anchor(self):
        out = build_prompt(
            file_contents="function bar(){}\n",
            function_name="bar",
            language="js",
            n_lines=20,
        )
        assert "function bar(" in out
        assert "opening brace" in out  # JS anchor wording

    def test_suppress_thinking_off(self):
        out = build_prompt(
            file_contents="x",
            function_name="f",
            language="py",
            n_lines=5,
            suppress_thinking=False,
        )
        assert "/no_think" not in out

    def test_multi_file_qualifier(self):
        out = build_prompt(
            file_contents="x",
            function_name="f",
            language="py",
            n_lines=5,
            source_path="a/b/foo.py",
            multi_file=True,
        )
        assert "in file `a/b/foo.py`" in out


class TestScorerIntegration:
    """Smoke-level checks that the vendored scorer behaves as documented."""

    def test_perfect_match_passes(self):
        primary = [f"    line {i}" for i in range(20)]
        bonus = [f"    bonus {i}" for i in range(10)]
        predicted = "\n".join(primary)
        s = score_function(name="f", primary=primary, bonus=bonus, predicted_text=predicted)
        assert s.passed
        assert s.primary_matched == 20
        assert s.bonus_matched == 0
        assert s.hallucinated == 0

    def test_partial_match_below_threshold(self):
        primary = [f"    line {i}" for i in range(20)]
        bonus = []
        # Reproduce only the first 5 lines — below PASS_THRESHOLD=8
        predicted = "\n".join(primary[:5])
        s = score_function(name="f", primary=primary, bonus=bonus, predicted_text=predicted)
        assert not s.passed
        assert s.primary_matched == 5

    def test_partial_match_at_threshold_passes(self):
        primary = [f"    line {i}" for i in range(20)]
        bonus = []
        predicted = "\n".join(primary[:8])  # exactly 8 matched = PASS_THRESHOLD
        s = score_function(name="f", primary=primary, bonus=bonus, predicted_text=predicted)
        assert s.passed
        assert s.primary_matched == 8

    def test_hallucinations_counted(self):
        primary = [f"    line {i}" for i in range(20)]
        predicted = "\n".join(primary) + "\n    UNRELATED LINE\n    ANOTHER FAKE"
        s = score_function(name="f", primary=primary, bonus=[], predicted_text=predicted)
        assert s.hallucinated == 2

    def test_bonus_credited(self):
        primary = [f"    line {i}" for i in range(20)]
        bonus = [f"    bonus {i}" for i in range(10)]
        predicted = "\n".join(primary + bonus)
        s = score_function(name="f", primary=primary, bonus=bonus, predicted_text=predicted)
        assert s.bonus_matched == 10


class TestAggregateItems:
    def test_empty(self):
        assert aggregate_items([]) == {"count": 0, "pass_rate": 0.0}

    def test_basic_aggregation(self):
        items = [
            {"passed": True, "primary_matched": 18, "primary_total": 20, "bonus_matched": 5, "hallucinated": 0},
            {"passed": False, "primary_matched": 3, "primary_total": 20, "bonus_matched": 0, "hallucinated": 7},
            {"passed": True, "primary_matched": 10, "primary_total": 20, "bonus_matched": 2, "hallucinated": 1},
        ]
        s = aggregate_items(items)
        assert s["count"] == 3
        assert s["passed"] == 2
        assert s["pass_rate"] == pytest.approx(2 / 3)
        assert s["primary_matched"] == 31
        assert s["primary_total"] == 60
        assert s["bonus_matched"] == 7
        assert s["hallucinated"] == 8


class TestManifest:
    """The committed manifest is part of codeneedle/v1; verify it's well-formed."""

    @pytest.fixture(scope="class")
    def manifest(self):
        if not MANIFEST.exists():
            pytest.skip(f"manifest not built: run scripts/build_codeneedle_manifest.py")
        return json.loads(MANIFEST.read_text())

    def test_protocol_version(self, manifest):
        assert manifest["protocol_version"] == "codeneedle/v1"

    def test_has_both_corpora(self, manifest):
        names = {c["corpus_name"] for c in manifest["corpora"]}
        assert "http_server.py" in names
        assert "jquery.js" in names

    def test_each_function_has_primary_lines(self, manifest):
        for c in manifest["corpora"]:
            for fn in c["functions"]:
                assert "primary_lines" in fn
                # primary_lines should be exactly MIN_BODY_LINES (=20) for valid sets
                assert len(fn["primary_lines"]) == 20, (
                    f"{c['corpus_name']}/{fn['name']} has {len(fn['primary_lines'])} primary lines, expected 20"
                )

    def test_sampling_is_within_k(self, manifest):
        # k_target is 16; k_sampled may be less if the corpus has fewer eligible fns
        for c in manifest["corpora"]:
            assert c["k_sampled"] <= c["k_target"]
            assert c["k_sampled"] == len(c["functions"])
