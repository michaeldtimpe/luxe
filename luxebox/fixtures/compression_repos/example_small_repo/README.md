# example_small_repo

A small pure-Python utility library used as a fixture for the
compression benchmark. Each top-level module provides a narrow slice of
functionality; `tests/` has one test file per module exercising the
intended behaviour.

## Modules

- `mathops` — numeric helpers: `sum_all`, `product`. Sums and products
  over iterables; the guarantee is that operations are defined for all
  numeric inputs including negatives and zero.
- `strings` — text helpers: `slugify`, `truncate`, `title_case`.
  Slugify normalises whitespace and drops non-ascii; title_case leaves
  small words lowercase except at the start.
- `dates` — datetime helpers: `parse_iso_date`, `days_between`,
  `add_business_days`. ISO-8601 parsing preserves timezone info when
  supplied; business-day math skips Sat/Sun.
- `collections_utils` — collection helpers: `dedupe_preserve_order`,
  `chunk`, `flatten`, `group_by`. Dedupe is stable and handles both
  hashable and unhashable items.
- `stats` — statistics: `mean`, `median`, `variance`, `percentile`.
  Percentile uses linear interpolation; empty-input behaviour raises
  `ValueError` consistently across all functions.

## Running tests

```
pytest -q                           # full suite
pytest -q tests/test_dates.py       # one module
```

The suite is intentionally kept dependency-free so fixtures can be
copied into tempdirs by the benchmark runner without venv setup.
