"""Mechanical smoke inspection for a SWE-bench `predictions.json`.

Two modes:

1. **Mechanical** (default): four binary gates — non-empty patch, no
   new files, no test-path edits, substantive +/- content. Catches the
   prompt-regression failure modes (reproducer scripts, test-only edits,
   whitespace-only "fixes").

2. **Gold-proximity** (`--gold-source`): the n=10 A/B exposed that
   mechanical 10/10 PASS overstated quality (real-fix rate was 4/10).
   This mode adds five signals against the gold patch from SWE-bench
   Verified to produce a tiered verdict (strong / plausible / wrong_shape
   / wrong_function / wrong_target). Does NOT replace the Docker harness;
   purpose is to keep iteration honest without paying the harness cost.

The gold-proximity dimensions, with their thresholds:
  - Files touched: STRICT binary set match
  - Hunk location overlap: at least one (file, hunk_start_line) in the
    model patch is within 20 lines of a gold hunk in the same file.
    Replaces the earlier @@-text comparison (broken by diff-generator
    annotation drift — matplotlib's exact gold-match was failing because
    git wrote `@@ ... optional.` while the gold had `@@ ... def hist(...)`
    despite both hunks starting at line 6686).
  - Hunk count: PERCENTAGE — within ±50% of gold's hunk count
  - Patch size (total +/- lines): PERCENTAGE — within 3× of gold
  - Token overlap on added lines: SOFT — Jaccard ≥ 0.3

Strict on localization (binary, hard to argue with), tolerant on shape
(Jaccard catches close-but-different rewrites), bounded on size
(percentage cap on both directions — over-editing AND under-coverage).

Usage:
    python -m benchmarks.swebench.smoke_inspect \\
        --predictions acceptance/swebench/<dir>/predictions.json
    python -m benchmarks.swebench.smoke_inspect \\
        --predictions acceptance/swebench/<dir>/predictions.json \\
        --gold-source benchmarks/swebench/subsets/raw/verified.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|testing)(?:/|$)"
    r"|(?:^|/)test_[^/]*\.py$"
    r"|(?:^|/)[^/]*_test\.py$"
)

_TOKEN_RE = re.compile(r"\w+")


@dataclass
class InstanceVerdict:
    instance_id: str
    passed: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class GoldComparisonVerdict:
    """Richer verdict that compares the model patch against the SWE-bench
    Verified gold patch on five dimensions. Falls back to `tier` ==
    one of the mechanical reasons (e.g. "empty_patch") when the patch
    fails the basic gates."""
    instance_id: str
    tier: str
    signals: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _diff_paths(model_patch: str) -> list[str]:
    """Extract `b/<path>` paths from `diff --git a/... b/...` lines."""
    paths: list[str] = []
    for line in model_patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                paths.append(parts[1].strip())
    return paths


def _has_new_file(model_patch: str) -> bool:
    return any(line.startswith("new file mode") for line in model_patch.splitlines())


def _has_substantive_change(model_patch: str) -> bool:
    """At least one +/- line that is not blank, not a comment, not a +++/--- header."""
    for line in model_patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if not (line.startswith("+") or line.startswith("-")):
            continue
        body = line[1:].strip()
        if not body:
            continue
        # Python comment lines — would also catch hash-comments in YAML/conf,
        # which is fine; we want substantive logic edits.
        if body.startswith("#"):
            continue
        return True
    return False


def _hunk_count(patch: str) -> int:
    return sum(1 for ln in patch.splitlines() if ln.startswith("@@"))


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)")


def _hunk_locations(patch: str) -> set[tuple[str, int]]:
    """Extract (file_path, hunk_start_line) per hunk. Robust replacement
    for the earlier `@@ ... @@` text-matching approach: line numbers are
    stable across diff generators (since both gold and model patches
    are diffed against the same base_commit), whereas the post-@@ text
    annotation depends on git's heuristic for picking a "function header"
    from surrounding lines and disagrees frequently across patch sources.
    """
    out: set[tuple[str, int]] = set()
    current_file: str | None = None
    for ln in patch.splitlines():
        if ln.startswith("diff --git "):
            parts = ln.split(" b/", 1)
            current_file = parts[1].strip() if len(parts) == 2 else None
        elif ln.startswith("@@") and current_file:
            m = _HUNK_HEADER_RE.match(ln)
            if m:
                out.add((current_file, int(m.group(1))))
    return out


def _hunks_proximate(
    model_locs: set[tuple[str, int]],
    gold_locs: set[tuple[str, int]],
    tolerance: int = 20,
) -> bool:
    """At least one model hunk is within `tolerance` lines of a gold
    hunk in the same file. Empty gold = unmeasurable, give the benefit
    of the doubt. Tolerance accommodates same-method placement drift
    (e.g., flask-5014: model line 193 vs gold line 190 — both inside
    `__init__`, just different positions within it)."""
    if not gold_locs:
        return True
    for mf, ml in model_locs:
        for gf, gl in gold_locs:
            if mf == gf and abs(ml - gl) <= tolerance:
                return True
    return False


def _hunk_coverage(
    model_locs: set[tuple[str, int]],
    gold_locs: set[tuple[str, int]],
    tolerance: int = 20,
) -> float:
    """Fraction of gold hunks for which the model has at least one
    co-located hunk (within `tolerance` lines, same file). Catches
    multi-site fix coverage: when gold has 2+ hunks and the model only
    fixes one of them (requests-2931, astropy-13453), this drops below
    1.0 and the caller can demote the verdict from `strong` to
    `plausible`. Returns 1.0 when gold has no hunks (unmeasurable)."""
    if not gold_locs:
        return 1.0
    covered = sum(
        1 for gf, gl in gold_locs
        if any(mf == gf and abs(ml - gl) <= tolerance
               for mf, ml in model_locs)
    )
    return covered / len(gold_locs)


def _added_line_tokens(patch: str) -> set[str]:
    """Word-level tokens from added (`+`) lines, excluding the `+++` header."""
    tokens: set[str] = set()
    for ln in patch.splitlines():
        if ln.startswith("+++") or not ln.startswith("+"):
            continue
        tokens.update(_TOKEN_RE.findall(ln))
    return tokens


def _patch_size(patch: str) -> int:
    """Total +/- line count, excluding +++/--- headers."""
    plus = sum(1 for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    minus = sum(1 for ln in patch.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return plus + minus


def _hunk_count_within_tolerance(model_hunks: int, gold_hunks: int) -> bool:
    """Within ±50% of gold, with a min lower bound of 1 (since 0 is
    handled separately as empty) and a min upper bound of 2 (so a
    gold=1 instance accepts both 1 and 2 hunks)."""
    if gold_hunks <= 0:
        return model_hunks == 0
    lo = max(1, gold_hunks // 2)
    hi = max(2, (gold_hunks * 3) // 2)
    return lo <= model_hunks <= hi


def _patch_size_within_tolerance(model_size: int, gold_size: int) -> bool:
    """Total +/- within 3× of gold (in either direction). For very small
    gold patches (1-2 lines), 3× is generous which is fine — we're trying
    to reject 30-line bloat, not penalize tight fixes."""
    if gold_size <= 0:
        return model_size == 0
    return gold_size / 3 <= model_size <= gold_size * 3


def inspect_instance(instance_id: str, model_patch: str) -> InstanceVerdict:
    reasons: list[str] = []
    if not model_patch.strip():
        reasons.append("empty_patch")
        return InstanceVerdict(instance_id=instance_id, passed=False, reasons=reasons)

    if _has_new_file(model_patch):
        reasons.append("new_file_in_diff")

    paths = _diff_paths(model_patch)
    test_paths = [p for p in paths if _TEST_PATH_RE.search(p)]
    if test_paths:
        reasons.append(f"touches_test_paths={test_paths}")

    if not _has_substantive_change(model_patch):
        reasons.append("no_substantive_change")

    return InstanceVerdict(
        instance_id=instance_id,
        passed=not reasons,
        reasons=reasons,
    )


def compare_to_gold(
    instance_id: str, model_patch: str, gold_patch: str
) -> GoldComparisonVerdict:
    """Compute the gold-proximity verdict. Defers to `inspect_instance`
    for the basic mechanical gates — if any of those fire (empty,
    new_file_in_diff, touches_test_paths, no_substantive_change), the
    tier is the first such reason and the rich signals are skipped."""
    base = inspect_instance(instance_id, model_patch)
    if not base.passed:
        return GoldComparisonVerdict(
            instance_id=instance_id,
            tier=base.reasons[0],
            reasons=base.reasons,
        )

    model_files = set(_diff_paths(model_patch))
    gold_files = set(_diff_paths(gold_patch))
    files_match = model_files == gold_files

    model_locs = _hunk_locations(model_patch)
    gold_locs = _hunk_locations(gold_patch)
    location_match = _hunks_proximate(model_locs, gold_locs)
    coverage = _hunk_coverage(model_locs, gold_locs)
    full_coverage = coverage >= 1.0

    gold_hunks = _hunk_count(gold_patch)
    model_hunks = _hunk_count(model_patch)
    hunk_count_ok = _hunk_count_within_tolerance(model_hunks, gold_hunks)

    gold_size = _patch_size(gold_patch)
    model_size = _patch_size(model_patch)
    size_ok = _patch_size_within_tolerance(model_size, gold_size)

    gold_tokens = _added_line_tokens(gold_patch)
    model_tokens = _added_line_tokens(model_patch)
    union = gold_tokens | model_tokens
    if union:
        jaccard = len(gold_tokens & model_tokens) / len(union)
    else:
        jaccard = 0.0
    token_overlap_ok = jaccard >= 0.3

    signals = {
        "files_match": files_match,
        "location_match": location_match,
        "full_coverage": full_coverage,
        "hunk_count_ok": hunk_count_ok,
        "size_ok": size_ok,
        "token_overlap_ok": token_overlap_ok,
        "model_hunks": model_hunks,
        "gold_hunks": gold_hunks,
        "model_size": model_size,
        "gold_size": gold_size,
        "jaccard": round(jaccard, 3),
        "coverage": round(coverage, 3),
    }

    # `strong` requires all five proximity signals AND full coverage of
    # gold's hunks. Partial-coverage cases (1 of 2 gold hunks touched)
    # demote to `plausible` even when each touched hunk looks great in
    # isolation — the multi-site consistency gap is the (b2) failure
    # the manual review surfaced as the dominant pattern at n=10.
    if not files_match:
        tier = "wrong_target"
    elif not location_match:
        tier = "wrong_location"
    elif (full_coverage and hunk_count_ok and size_ok and token_overlap_ok):
        tier = "strong"
    elif (full_coverage and any([hunk_count_ok, size_ok, token_overlap_ok])):
        tier = "plausible"
    elif any([hunk_count_ok, size_ok, token_overlap_ok]):
        # Located in the right area, partially covers gold, some shape
        # signals green — partial fix.
        tier = "plausible"
    else:
        tier = "wrong_shape"

    reasons: list[str] = []
    if not files_match:
        reasons.append(
            f"model_files={sorted(model_files)} gold_files={sorted(gold_files)}"
        )
    if files_match and not location_match:
        reasons.append(
            f"model_locs={sorted(model_locs)} gold_locs={sorted(gold_locs)}"
        )
    if files_match and location_match and not full_coverage:
        reasons.append(f"coverage={coverage:.2f} (model touches "
                       f"{int(coverage * len(gold_locs))}/"
                       f"{len(gold_locs)} gold hunks)")
    if files_match and location_match and not hunk_count_ok:
        reasons.append(f"hunks={model_hunks}/gold={gold_hunks}")
    if files_match and location_match and not size_ok:
        reasons.append(f"size={model_size}/gold={gold_size}")
    if files_match and location_match and not token_overlap_ok:
        reasons.append(f"jaccard={jaccard:.2f}")

    return GoldComparisonVerdict(
        instance_id=instance_id,
        tier=tier,
        signals=signals,
        reasons=reasons,
    )


def inspect_predictions(predictions_path: Path) -> list[InstanceVerdict]:
    rows = json.loads(predictions_path.read_text())
    return [inspect_instance(r["instance_id"], r.get("model_patch", "")) for r in rows]


def load_gold_patches(gold_source: Path) -> dict[str, str]:
    """Load `instance_id → patch` from a SWE-bench Verified JSONL or
    JSON file. Reads the whole file into memory (small enough)."""
    text = gold_source.read_text()
    out: dict[str, str] = {}
    if gold_source.suffix == ".jsonl":
        for line in text.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            out[row["instance_id"]] = row.get("patch", "")
    else:
        rows = json.loads(text)
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            out[row["instance_id"]] = row.get("patch", "")
    return out


def compare_predictions_to_gold(
    predictions_path: Path, gold_source: Path
) -> list[GoldComparisonVerdict]:
    rows = json.loads(predictions_path.read_text())
    gold = load_gold_patches(gold_source)
    out: list[GoldComparisonVerdict] = []
    for r in rows:
        iid = r["instance_id"]
        gold_patch = gold.get(iid, "")
        out.append(compare_to_gold(iid, r.get("model_patch", ""), gold_patch))
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--predictions", required=True, type=Path)
    p.add_argument(
        "--gold-source",
        type=Path,
        help="JSONL or JSON with gold patches (e.g., benchmarks/swebench/"
        "subsets/raw/verified.jsonl). When provided, prints the rich "
        "5-signal gold-proximity tier in addition to mechanical PASS/FAIL.",
    )
    args = p.parse_args()

    verdicts = inspect_predictions(args.predictions)
    n_pass = sum(1 for v in verdicts if v.passed)
    n_total = len(verdicts)

    if not args.gold_source:
        for v in verdicts:
            mark = "PASS" if v.passed else "FAIL"
            reasons = "" if v.passed else f"  reasons={v.reasons}"
            print(f"  {mark}  {v.instance_id}{reasons}")
        print()
        print(f"smoke inspect: {n_pass}/{n_total} pass")
        return 0 if n_pass == n_total else 1

    # Gold-proximity mode
    gold_verdicts = compare_predictions_to_gold(args.predictions, args.gold_source)
    tier_counts: dict[str, int] = {}
    for gv in gold_verdicts:
        tier_counts[gv.tier] = tier_counts.get(gv.tier, 0) + 1

    for gv in gold_verdicts:
        sig_short = ""
        if gv.signals:
            flags = []
            for k in ("files_match", "location_match", "full_coverage",
                      "hunk_count_ok", "size_ok", "token_overlap_ok"):
                flags.append(f"{k.split('_')[0][:4]}={'Y' if gv.signals.get(k) else 'N'}")
            sig_short = "  " + " ".join(flags)
            sig_short += f"  cov={gv.signals.get('coverage', 0):.2f}"
            sig_short += f"  jac={gv.signals.get('jaccard', 0):.2f}"
        reasons = f"  ({', '.join(gv.reasons)})" if gv.reasons and gv.tier != "strong" else ""
        print(f"  {gv.tier:14s}  {gv.instance_id}{sig_short}{reasons}")
    print()
    n_strong = tier_counts.get("strong", 0)
    n_plausible = tier_counts.get("plausible", 0)
    print(f"gold-proximity: strong={n_strong}, plausible={n_plausible}, "
          f"wrong_shape={tier_counts.get('wrong_shape', 0)}, "
          f"wrong_location={tier_counts.get('wrong_location', 0)}, "
          f"wrong_target={tier_counts.get('wrong_target', 0)}")
    print(f"  mechanical PASS: {n_pass}/{n_total}; "
          f"strong-or-plausible: {n_strong + n_plausible}/{n_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
