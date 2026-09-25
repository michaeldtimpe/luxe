"""Offline tests for MMLU --limit stratification. No model, no network."""
from __future__ import annotations

from benchmarks.mmlu.adapter import select_stratified


def _rows(sizes: dict[str, int]) -> list[dict]:
    """Build rows for subjects with the given sizes, in insertion order."""
    rows = []
    for subj, n in sizes.items():
        for i in range(n):
            rows.append({"subject": subj, "qid": i})
    return rows


class TestSelectStratified:
    def test_limit_none_returns_all_rows(self):
        rows = _rows({"anatomy": 3, "astronomy": 2})
        out = select_stratified(rows, None)
        assert out == rows

    def test_limit_equal_to_total_returns_all_rows(self):
        rows = _rows({"anatomy": 3, "astronomy": 2})
        out = select_stratified(rows, len(rows))
        assert len(out) == len(rows)
        assert sorted((r["subject"], r["qid"]) for r in out) == sorted(
            (r["subject"], r["qid"]) for r in rows
        )

    def test_limit_greater_than_total_returns_all_rows(self):
        rows = _rows({"anatomy": 3, "astronomy": 2})
        out = select_stratified(rows, 10_000)
        assert len(out) == len(rows)

    def test_full_test_set_size_selects_everything(self):
        # Regression: the real MMLU test set is 14042 rows across 57
        # subjects; the old per-subject `rows[:take]` truncation collapsed
        # `--limit 14042` down to 10158 rows because it never redistributed
        # a small subject's unused quota. Model it with lopsided sizes.
        sizes = {f"subj_{i:02d}": (5 if i % 3 == 0 else 400) for i in range(57)}
        rows = _rows(sizes)
        out = select_stratified(rows, len(rows))
        assert len(out) == len(rows)

    def test_small_limit_is_evenly_stratified(self):
        rows = _rows({"anatomy": 10, "astronomy": 10, "biology": 10})
        out = select_stratified(rows, 9)
        assert len(out) == 9
        counts: dict[str, int] = {}
        for r in out:
            counts[r["subject"]] = counts.get(r["subject"], 0) + 1
        assert counts == {"anatomy": 3, "astronomy": 3, "biology": 3}

    def test_uneven_subjects_redistribute_shortfall(self):
        # sizes 1, 5, 100 with limit 30: the small subjects can't fill an
        # even 10-each share, so their leftover quota must go to the large
        # subject rather than under-selecting.
        rows = _rows({"a_tiny": 1, "b_small": 5, "c_big": 100})
        out = select_stratified(rows, 30)
        assert len(out) == 30
        counts: dict[str, int] = {}
        for r in out:
            counts[r["subject"]] = counts.get(r["subject"], 0) + 1
        assert counts["a_tiny"] == 1  # exhausted, all of it taken
        assert counts["b_small"] == 5  # exhausted, all of it taken
        assert counts["c_big"] == 24  # absorbs the redistributed shortfall
        assert sum(counts.values()) == 30

    def test_deterministic(self):
        rows = _rows({"a_tiny": 1, "b_small": 5, "c_big": 100})
        out1 = select_stratified(rows, 30)
        out2 = select_stratified(rows, 30)
        assert out1 == out2

    def test_rows_kept_in_original_per_subject_order(self):
        rows = _rows({"anatomy": 5})
        out = select_stratified(rows, 3)
        assert [r["qid"] for r in out] == [0, 1, 2]

    def test_output_grouped_by_sorted_subject(self):
        # limit < total so the stratify path (not the "use all rows" fast
        # path) is exercised, which groups output by sorted subject.
        rows = _rows({"zoology": 2, "anatomy": 2})
        out = select_stratified(rows, 3)
        assert [r["subject"] for r in out] == ["anatomy", "anatomy", "zoology"]
