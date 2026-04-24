"""Tests for the compression_repo benchmark and strategy pipeline.

Exercises the moving parts that don't need a live model: strategy
loading, stage execution, retrieval ranking, patch-apply + pytest grade,
and failure modes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.compression_repo import CompressionRepo, _file_precision_recall
from harness.metrics import RunMetrics
from strategies import Context, load_strategy, run_pipeline

FIXTURE_REPO = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "compression_repos"
    / "example_small_repo"
)

GOOD_DIFF = """```diff
--- a/mathops.py
+++ b/mathops.py
@@ -3,8 +3,7 @@
 def sum_all(nums):
     total = 0
     for n in nums:
-        if n > 0:
-            total += n
+        total += n
     return total


 def product(nums):
```
"""

BAD_DIFF = """```diff
--- a/mathops.py
+++ b/mathops.py
@@ -99,1 +99,1 @@
-does not exist
+will not apply
```
"""


# A diff with the wrong starting line number (@@ -10 instead of -3): the
# file is only ~14 lines long, so the hunk header is off. `patch -p1`
# rejects this; `git apply --3way` should recover via 3-way merge.
OFF_BY_LINE_DIFF = """```diff
--- a/mathops.py
+++ b/mathops.py
@@ -10,8 +10,7 @@
 def sum_all(nums):
     total = 0
     for n in nums:
-        if n > 0:
-            total += n
+        total += n
     return total


 def product(nums):
```
"""


# A diff with correct line content but wrong hunk line counts — the
# dominant real-world failure mode observed on qwen2.5-coder. Both
# `git apply` and `patch(1)` reject this; only the context-match
# fallback should accept it.
WRONG_COUNT_DIFF = """```diff
--- a/mathops.py
+++ b/mathops.py
@@ -4,99 +4,99 @@ def sum_all(nums):
     total = 0
     for n in nums:
-        if n > 0:
-            total += n
+        total += n
     return total
```
"""


WHOLE_FILE_COMPLETION = """Here's the fixed version.

```python
# FILE: mathops.py
\"\"\"Small numeric helpers used by tests.\"\"\"


def sum_all(nums):
    total = 0
    for n in nums:
        total += n
    return total


def product(nums):
    out = 1
    for n in nums:
        out *= n
    return out
```
"""


def _baseline_ctx(reference: dict | None = None) -> Context:
    return Context(
        repo_root=FIXTURE_REPO,
        task_description="sum_all drops negative numbers; make it include them",
        goal="pytest passes",
        reference=reference or {},
    )


# ── strategy loading ────────────────────────────────────────────────


def test_load_baseline_strategy_by_name():
    strat = load_strategy("baseline_retrieval_only")
    assert strat["name"] == "baseline_retrieval_only"
    stage_ids = [s["id"] for s in strat["stages"]]
    assert stage_ids == ["preprocess", "index", "retrieve", "compress", "prompt_assembly"]


def test_load_strategy_unknown_stage_raises():
    with pytest.raises(KeyError, match="unknown stage"):
        run_pipeline({"stages": [{"id": "nonsense", "enabled": True}]}, _baseline_ctx())


# ── stage pipeline ──────────────────────────────────────────────────


def test_pipeline_populates_context():
    ctx = _baseline_ctx()
    run_pipeline(load_strategy("baseline_retrieval_only"), ctx)

    # Retrieval surfaced the source file. We deliberately don't pin the
    # exact rank: the baseline is a crude keyword overlap, and README.md
    # can tie or beat mathops.py because it mentions the same terms —
    # which is exactly the "baseline is weak" signal smarter strategies
    # get measured against.
    rel = [p.relative_to(FIXTURE_REPO).as_posix() for p in ctx.files_in_scope]
    assert "mathops.py" in rel

    # Messages assembled; retrieval timing recorded.
    assert ctx.messages and ctx.messages[0]["role"] == "system"
    assert ctx.assembled_prompt_chars > 0
    assert ctx.t_retrieval_s > 0


def test_disabled_stages_are_skipped():
    ctx = _baseline_ctx()
    # Only enable prompt_assembly; nothing retrieves, so the prompt has
    # no "Relevant files:" section and files_in_scope stays empty.
    run_pipeline(
        {"stages": [
            {"id": "index", "enabled": False},
            {"id": "retrieve", "enabled": False},
            {"id": "prompt_assembly", "enabled": True},
        ]},
        ctx,
    )
    assert ctx.files_in_scope == []
    assert ctx.indexed_files == []
    user_msg = ctx.messages[1]["content"]
    assert "Relevant files:" not in user_msg


# ── precision / recall ──────────────────────────────────────────────


def test_file_precision_recall_basic():
    p, r = _file_precision_recall(
        selected=["mathops.py", "README.md", "tests/test_math.py", "conftest.py"],
        relevant=["mathops.py"],
    )
    assert p == pytest.approx(0.25)
    assert r == pytest.approx(1.0)


def test_file_precision_recall_empty():
    assert _file_precision_recall([], []) == (0.0, 0.0)


# ── benchmark: tasks + build_messages + grade ───────────────────────


def _bench() -> CompressionRepo:
    return CompressionRepo.from_strategy_name("baseline_retrieval_only")


def _get_task(bench: CompressionRepo, task_id: str):
    for t in bench.tasks(limit=None):
        if t.id == task_id:
            return t
    raise KeyError(task_id)


def test_tasks_discovers_fixture_task():
    tasks = list(_bench().tasks(limit=None))
    ids = [t.id for t in tasks]
    assert "bugfix_small_repo_001" in ids


def test_build_messages_stashes_pipeline_ctx():
    bench = _bench()
    task = _get_task(bench, "bugfix_small_repo_001")
    msgs = bench.build_messages(task)
    assert msgs and msgs[-1]["role"] == "user"
    ctx = task.metadata["_pipeline_ctx"]
    assert isinstance(ctx, Context)
    assert ctx.messages is msgs


def test_grade_applies_good_diff_and_passes():
    bench = _bench()
    task = _get_task(bench, "bugfix_small_repo_001")
    bench.build_messages(task)  # populate pipeline ctx
    result = bench.grade(task, GOOD_DIFF, [])

    assert result.passed is True
    assert result.score == 1.0
    assert result.error is None
    assert result.details["apply_ok"] is True
    assert result.details["validation_exit"] == 0
    assert result.details["file_recall"] == pytest.approx(1.0)
    assert result.details["selected_files"]
    assert result.details["relevant_files"] == ["mathops.py"]


def test_grade_no_diff_fails_cleanly():
    bench = _bench()
    task = _get_task(bench, "bugfix_small_repo_001")
    bench.build_messages(task)
    result = bench.grade(task, "I think the fix goes in mathops.py.", [])

    assert result.passed is False
    assert result.score == 0.0
    assert result.error and "no unified diff" in result.error
    assert result.details["apply_ok"] is False


def test_grade_malformed_diff_fails_cleanly():
    bench = _bench()
    task = _get_task(bench, "bugfix_small_repo_001")
    bench.build_messages(task)
    result = bench.grade(task, BAD_DIFF, [])

    assert result.passed is False
    assert result.details["apply_ok"] is False
    assert result.error and "patch apply failed" in result.error


def test_grade_recovers_from_off_by_line_diff():
    """git apply --3way should resolve a diff whose hunk header points
    at the wrong starting line, as long as the surrounding context
    matches somewhere in the file."""
    bench = _bench()
    task = _get_task(bench, "bugfix_small_repo_001")
    bench.build_messages(task)
    result = bench.grade(task, OFF_BY_LINE_DIFF, [])

    assert result.details["apply_ok"] is True, (
        f"expected 3-way merge to recover from off-by-line hunk, got error={result.error!r}"
    )
    assert result.passed is True
    assert result.details["validation_exit"] == 0


# ── retrieval methods ──────────────────────────────────────────────


def _run_strategy(name: str, reference: dict | None = None) -> Context:
    ctx = _baseline_ctx(reference=reference)
    run_pipeline(load_strategy(name), ctx)
    return ctx


def test_retrieve_none_leaves_scope_empty():
    ctx = _run_strategy("retrieve_none")
    assert ctx.files_in_scope == []
    user = ctx.messages[1]["content"]
    assert "Relevant files:" not in user


def test_retrieve_oracle_selects_exactly_reference_files():
    ctx = _run_strategy("retrieve_oracle", reference={"relevant_files": ["mathops.py"]})
    rel = [p.relative_to(FIXTURE_REPO).as_posix() for p in ctx.files_in_scope]
    assert rel == ["mathops.py"]


def test_retrieve_oracle_skips_missing_files():
    ctx = _run_strategy("retrieve_oracle", reference={"relevant_files": ["does_not_exist.py"]})
    assert ctx.files_in_scope == []


def test_retrieve_full_selects_all_indexed_files():
    ctx = _run_strategy("retrieve_full_context")
    # Full means: every text file in the repo made it through.
    assert len(ctx.files_in_scope) == len(ctx.indexed_files)
    assert len(ctx.files_in_scope) >= 3  # mathops, test_math, README, conftest


def test_retrieve_unknown_method_raises():
    strat = {
        "stages": [
            {"id": "retrieve", "enabled": True, "params": {"method": "ocrale"}},
        ]
    }
    with pytest.raises(KeyError, match="unknown retrieve method"):
        run_pipeline(strat, _baseline_ctx())


def test_benchmark_passes_reference_to_pipeline():
    """CompressionRepo should forward task reference into the Context
    so oracle retrieval can read relevant_files."""
    bench = CompressionRepo.from_strategy_name("retrieve_oracle")
    task = _get_task(bench, "bugfix_small_repo_001")
    bench.build_messages(task)
    ctx = task.metadata["_pipeline_ctx"]
    rel = [p.relative_to(FIXTURE_REPO).as_posix() for p in ctx.files_in_scope]
    assert rel == ["mathops.py"]


# ── context-matching patch fallback ────────────────────────────────


def test_context_match_fallback_recovers_from_wrong_hunk_counts():
    """The dominant qwen2.5-coder failure mode: correct line content,
    wrong @@ counts. git apply and patch(1) both reject these; the
    context-match fallback should accept them."""
    bench = _bench()
    task = _get_task(bench, "bugfix_small_repo_001")
    bench.build_messages(task)
    result = bench.grade(task, WRONG_COUNT_DIFF, [])

    assert result.details["apply_ok"] is True, (
        f"context-match fallback should handle wrong hunk counts; got error={result.error!r}"
    )
    assert result.passed is True
    assert result.details["validation_exit"] == 0


# ── whole-file output format ───────────────────────────────────────


def test_prompt_assembly_whole_file_includes_file_header_instruction():
    ctx = Context(
        repo_root=FIXTURE_REPO,
        task_description="fix sum_all",
        goal="tests pass",
        reference={"relevant_files": ["mathops.py"]},
    )
    run_pipeline(load_strategy("retrieve_oracle_whole_file"), ctx)
    assert ctx.output_format == "whole_file"
    user = ctx.messages[1]["content"]
    assert "# FILE:" in user
    assert "unified diff" not in user


def test_whole_file_grade_overwrites_file_and_passes():
    bench = CompressionRepo.from_strategy_name("retrieve_oracle_whole_file")
    task = _get_task(bench, "bugfix_small_repo_001")
    bench.build_messages(task)
    result = bench.grade(task, WHOLE_FILE_COMPLETION, [])

    assert result.details["output_format"] == "whole_file"
    assert result.details["apply_ok"] is True
    assert result.passed is True
    assert result.details["validation_exit"] == 0


def test_whole_file_grade_no_file_blocks_fails_cleanly():
    bench = CompressionRepo.from_strategy_name("retrieve_oracle_whole_file")
    task = _get_task(bench, "bugfix_small_repo_001")
    bench.build_messages(task)
    result = bench.grade(task, "I would fix mathops.py but cannot.", [])

    assert result.passed is False
    assert result.details["apply_ok"] is False
    assert result.error and "FILE:" in result.error


# ── stack_trace_guided retrieval ───────────────────────────────────


def test_stack_trace_guided_retrieves_failing_file():
    """On the strings task, pytest fails with a traceback pointing at
    tests/test_strings.py and strings.py. Stack-trace retrieval should
    surface both."""
    bench = CompressionRepo.from_strategy_name("stack_trace_guided_wf")
    tasks = {t.id: t for t in bench.tasks(limit=None)}
    task = tasks["bugfix_slugify_whitespace"]
    bench.build_messages(task)
    ctx = task.metadata["_pipeline_ctx"]
    rel = [p.relative_to(FIXTURE_REPO).as_posix() for p in ctx.files_in_scope]
    assert "strings.py" in rel, f"stack-trace should surface strings.py; got {rel}"
    # Test file also appears because its path is in the traceback.
    assert any("tests/test_strings.py" == r or r.endswith("test_strings.py") for r in rel)


# ── file_outline_only compression ──────────────────────────────────


def test_file_outline_only_emits_signatures_not_bodies():
    bench = CompressionRepo.from_strategy_name("file_outline_only_wf")
    tasks = {t.id: t for t in bench.tasks(limit=None)}
    task = tasks["bugfix_slugify_whitespace"]
    bench.build_messages(task)
    ctx = task.metadata["_pipeline_ctx"]
    user = ctx.messages[1]["content"]
    # Should include function signatures...
    assert "def slugify" in user
    # ...but not the full body's actual implementation tokens.
    assert "_WHITESPACE.sub" not in user, (
        "outline compression should strip body, keeping only signatures + docstrings"
    )


# ── retrieve_then_summarize with fallback ──────────────────────────


def test_summarize_falls_back_to_outlines_without_backend():
    """When no backend is attached (unit tests), the summarize compressor
    should gracefully downgrade to outlines rather than crashing."""
    bench = CompressionRepo.from_strategy_name("retrieve_then_summarize_wf")
    # Benchmark has no .backend attribute in this context.
    tasks = {t.id: t for t in bench.tasks(limit=None)}
    task = tasks["bugfix_slugify_whitespace"]
    bench.build_messages(task)
    ctx = task.metadata["_pipeline_ctx"]
    # compressed_text populated (via outline fallback), and the user
    # message references outlines not full bodies.
    assert ctx.compressed_text is not None
    assert "(outline)" in ctx.compressed_text or "(summary)" in ctx.compressed_text


def test_whole_file_parser_accepts_unfenced_blocks():
    """qwen2.5-coder:32b observed dropping the ```python fence. Parser
    must still recover the body by anchoring on `# FILE:` lines."""
    bench = CompressionRepo.from_strategy_name("retrieve_oracle_whole_file")
    task = _get_task(bench, "bugfix_small_repo_001")
    bench.build_messages(task)
    completion = (
        "# FILE: mathops.py\n"
        '"""Small numeric helpers used by tests."""\n'
        "\n\n"
        "def sum_all(nums):\n"
        "    total = 0\n"
        "    for n in nums:\n"
        "        total += n\n"
        "    return total\n"
        "\n\n"
        "def product(nums):\n"
        "    out = 1\n"
        "    for n in nums:\n"
        "        out *= n\n"
        "    return out\n"
    )
    result = bench.grade(task, completion, [])
    assert result.details["apply_ok"] is True, f"unfenced whole-file should parse; err={result.error!r}"
    assert result.passed is True


# ── RunMetrics extension ────────────────────────────────────────────


def test_run_metrics_exposes_compression_fields():
    m = RunMetrics(candidate_id="c", config_id="cfg", benchmark="b", task_id="t")
    m.t_retrieval_s = 0.5
    m.t_compression_s = 0.25
    m.peak_context_tokens = 1234
    m.file_precision = 0.5
    m.file_recall = 0.75
    m.finish()

    d = m.to_dict()
    assert d["compression"] == {
        "t_retrieval_s": 0.5,
        "t_compression_s": 0.25,
        "peak_context_tokens": 1234,
        "file_precision": 0.5,
        "file_recall": 0.75,
    }


# ── strategy JSON on disk is well-formed ────────────────────────────


def test_baseline_strategy_json_parses():
    path = (
        Path(__file__).resolve().parent.parent
        / "strategies"
        / "configs"
        / "baseline_retrieval_only.json"
    )
    data = json.loads(path.read_text())
    assert data["name"] == "baseline_retrieval_only"
    assert all("id" in s for s in data["stages"])
