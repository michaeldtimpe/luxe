"""Tests for benchmarks/swebench/fixtures.py and stratify.py.

PRELIMINARY (2026-05-03). Validates the data model + stratification
recipe against synthetic SWE-bench rows. Real-dataset integration tests
deferred until HuggingFace `datasets` package install is approved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.swebench.fixtures import (
    SweBenchInstance,
    load_instances_from_json,
    _row_to_instance,
)
from benchmarks.swebench.stratify import (
    _gold_patch_file_count,
    _is_feature_style,
    read_subset,
    stratify,
    write_subset,
)


def _row(
    instance_id: str = "owner__repo-1",
    repo: str = "owner/repo",
    difficulty: str = "<15 min fix",
    problem: str = "fix the bug",
    patch: str = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new",
    fail_to_pass: list[str] | str | None = None,
    pass_to_pass: list[str] | str | None = None,
) -> dict:
    return {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": "deadbeef" * 5,
        "problem_statement": problem,
        "hints_text": "",
        "patch": patch,
        "test_patch": "",
        "FAIL_TO_PASS": fail_to_pass if fail_to_pass is not None else ["test_a"],
        "PASS_TO_PASS": pass_to_pass if pass_to_pass is not None else [],
        "environment_setup_commit": "",
        "version": "5.0",
        "difficulty": difficulty,
    }


def test_row_to_instance_decodes_string_test_arrays():
    row = _row(fail_to_pass='["test_a", "test_b"]', pass_to_pass='[]')
    inst = _row_to_instance(row)
    assert inst.fail_to_pass == ["test_a", "test_b"]
    assert inst.pass_to_pass == []


def test_row_to_instance_passes_list_test_arrays_through():
    row = _row(fail_to_pass=["test_a"], pass_to_pass=["test_b", "test_c"])
    inst = _row_to_instance(row)
    assert inst.fail_to_pass == ["test_a"]
    assert inst.pass_to_pass == ["test_b", "test_c"]


def test_repo_url_format():
    inst = _row_to_instance(_row(repo="astropy/astropy"))
    assert inst.repo_url == "https://github.com/astropy/astropy.git"


def test_goal_prompt_truncates_long_problem_statements():
    inst = _row_to_instance(_row(problem="x" * 5000))
    goal = inst.goal_prompt(max_chars=3000)
    assert len(goal) <= 3030  # 3000 + truncation marker
    assert "[truncated]" in goal


def test_goal_prompt_appends_hints():
    inst = SweBenchInstance(
        instance_id="i", repo="o/r", base_commit="x" * 40,
        problem_statement="fix it", hints_text="check helper.py",
    )
    goal = inst.goal_prompt()
    assert "fix it" in goal
    assert "Hints:" in goal
    assert "check helper.py" in goal


def test_load_from_json_list(tmp_path: Path):
    rows = [_row(instance_id="a__b-1"), _row(instance_id="a__b-2")]
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(rows))
    out = load_instances_from_json(path)
    assert len(out) == 2
    assert {i.instance_id for i in out} == {"a__b-1", "a__b-2"}


def test_load_from_jsonl(tmp_path: Path):
    rows = [_row(instance_id="a__b-1"), _row(instance_id="a__b-2")]
    path = tmp_path / "rows.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    out = load_instances_from_json(path)
    assert len(out) == 2


def test_is_feature_style_detects_feature_verbs():
    inst = _row_to_instance(_row(problem="Add support for foo"))
    assert _is_feature_style(inst) is True
    inst = _row_to_instance(_row(problem="implement bar"))
    assert _is_feature_style(inst) is True


def test_is_feature_style_detects_bugfix_phrasing():
    inst = _row_to_instance(_row(problem="When I call foo() the result is wrong"))
    assert _is_feature_style(inst) is False


def test_gold_patch_file_count():
    multi = _row(patch=(
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@\n-1\n+2\n"
        "diff --git a/y.py b/y.py\n--- a/y.py\n+++ b/y.py\n@@\n-1\n+2\n"
    ))
    assert _gold_patch_file_count(_row_to_instance(multi)) == 2

    single = _row(patch="diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@\n-1\n+2")
    assert _gold_patch_file_count(_row_to_instance(single)) == 1


def test_stratify_respects_per_repo_cap():
    rows = [_row(instance_id=f"big__big-{i}", repo="big/big") for i in range(20)]
    rows += [_row(instance_id=f"sml__sml-{i}", repo="sml/sml") for i in range(20)]
    instances = [_row_to_instance(r) for r in rows]
    ids = stratify(instances, n=16, per_repo_cap=8)
    big_count = sum(1 for x in ids if x.startswith("big__"))
    sml_count = sum(1 for x in ids if x.startswith("sml__"))
    assert big_count <= 8
    assert sml_count <= 8


def test_stratify_filters_out_long_difficulty():
    rows = [_row(instance_id=f"a__b-{i}", difficulty="<15 min fix") for i in range(10)]
    rows += [_row(instance_id=f"a__b-slow-{i}", difficulty="1-4 hours") for i in range(10)]
    instances = [_row_to_instance(r) for r in rows]
    ids = stratify(instances, n=10)
    for x in ids:
        assert "slow" not in x


def test_stratify_deterministic():
    rows = [_row(instance_id=f"r__r-{i}", difficulty="<15 min fix") for i in range(50)]
    instances = [_row_to_instance(r) for r in rows]
    a = stratify(instances, n=20, seed=42)
    b = stratify(instances, n=20, seed=42)
    assert a == b


def test_stratify_caps_at_pool_size():
    rows = [_row(instance_id=f"r__r-{i}") for i in range(5)]
    instances = [_row_to_instance(r) for r in rows]
    ids = stratify(instances, n=100)
    assert len(ids) <= 5


def test_write_and_read_subset_roundtrip(tmp_path: Path):
    ids = ["a__b-1", "a__b-2", "c__d-3"]
    path = tmp_path / "subset.json"
    write_subset(ids, path)
    assert read_subset(path) == ids
    payload = json.loads(path.read_text())
    assert payload["n"] == 3
    assert payload["recipe"]["per_repo_cap"] == 8
