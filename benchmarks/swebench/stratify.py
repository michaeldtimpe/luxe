"""Stratified subset selection for SWE-bench Verified.

PRELIMINARY (2026-05-03). Recipe per
`~/.claude/plans/fancy-honking-lerdorf.md`:
1. Filter Verified to `<15 min fix` and `15 min - 1 hour` difficulty.
2. Cap at 8 instances per repo (avoids django/sympy domination).
3. ~70% bugfix-style, ~30% feature-add-style (heuristic on
   problem_statement verbs).
4. ~60% single-file, ~40% multi-file (heuristic: gold-patch file count).
5. Output a stable, ordered list of instance_ids — frozen as
   `subsets/v1_baseline_n75.json` once approved.

The frozen list is the ONLY subset used for pre/post SpecDD comparison.
Re-running stratify.py with the same seed should regenerate it byte-for-byte.
"""

from __future__ import annotations

import json
import random
import re
from collections import defaultdict
from pathlib import Path

from .fixtures import SweBenchInstance


_FEATURE_VERBS = re.compile(
    r"\b(add|implement|support|introduce|enable|allow|create|provide|"
    r"feature|enhancement)\b",
    re.IGNORECASE,
)

_FAST_DIFFICULTIES = {"<15 min fix", "15 min - 1 hour"}


def _is_feature_style(instance: SweBenchInstance) -> bool:
    """Heuristic: feature-add tasks tend to use additive verbs in the
    first paragraph of problem_statement. Bugfix tasks tend to start
    with a complaint or repro. Imperfect — surface in the frozen list
    so the user can hand-correct."""
    head = instance.problem_statement.split("\n\n", 1)[0]
    return bool(_FEATURE_VERBS.search(head))


def _gold_patch_file_count(instance: SweBenchInstance) -> int:
    """Count distinct files touched by the gold patch. Used as a proxy
    for problem complexity. Returns 1 when patch is empty (defensive)."""
    if not instance.patch:
        return 1
    return len(set(re.findall(r"^diff --git a/(\S+)", instance.patch, re.MULTILINE)))


def stratify(
    instances: list[SweBenchInstance],
    *,
    n: int = 75,
    per_repo_cap: int = 8,
    feature_ratio: float = 0.30,
    multi_file_ratio: float = 0.40,
    seed: int = 20260503,
) -> list[str]:
    """Select a stratified subset and return ordered instance_ids.

    Deterministic given (instances, seed). Order within ties is by
    instance_id ASCII sort to match HF dataset enumeration. The output
    list is the canonical pre/post comparison subset — write to
    `subsets/v1_baseline_n75.json` and treat as read-only thereafter.
    """
    rng = random.Random(seed)

    pool = [i for i in instances if i.difficulty in _FAST_DIFFICULTIES]
    pool.sort(key=lambda i: i.instance_id)

    by_repo: dict[str, list[SweBenchInstance]] = defaultdict(list)
    for i in pool:
        by_repo[i.repo].append(i)

    target_features = int(round(n * feature_ratio))
    target_bugfix = n - target_features
    target_multi = int(round(n * multi_file_ratio))
    target_single = n - target_multi

    selected: list[SweBenchInstance] = []
    selected_by_repo: dict[str, int] = defaultdict(int)
    feature_count = 0
    multi_count = 0

    repo_order = sorted(by_repo)
    rng.shuffle(repo_order)

    while len(selected) < n:
        progress = False
        for repo in repo_order:
            if len(selected) >= n:
                break
            if selected_by_repo[repo] >= per_repo_cap:
                continue
            for cand in by_repo[repo]:
                if cand in selected:
                    continue
                is_feature = _is_feature_style(cand)
                is_multi = _gold_patch_file_count(cand) >= 2

                # Soft balancing — prefer candidates that move us toward
                # the target ratios without exceeding them. Falls through
                # if no perfectly-fitting candidate exists, taking the
                # first compatible one.
                if is_feature and feature_count >= target_features:
                    continue
                if not is_feature and (len(selected) - feature_count) >= target_bugfix:
                    continue
                if is_multi and multi_count >= target_multi:
                    continue
                if not is_multi and (len(selected) - multi_count) >= target_single:
                    continue

                selected.append(cand)
                selected_by_repo[repo] += 1
                if is_feature:
                    feature_count += 1
                if is_multi:
                    multi_count += 1
                progress = True
                break  # advance to the next repo before picking a 2nd from this one
        if not progress:
            break

    # Backfill if soft-balancing left us short. Re-check the per-repo
    # cap inside the loop so additions during backfill don't exceed it.
    if len(selected) < n:
        selected_ids = {i.instance_id for i in selected}
        for cand in pool:
            if len(selected) >= n:
                break
            if cand.instance_id in selected_ids:
                continue
            if selected_by_repo[cand.repo] >= per_repo_cap:
                continue
            selected.append(cand)
            selected_ids.add(cand.instance_id)
            selected_by_repo[cand.repo] += 1

    selected.sort(key=lambda i: i.instance_id)
    return [i.instance_id for i in selected]


def write_subset(instance_ids: list[str], output_path: Path | str) -> None:
    """Write the frozen subset list as JSON. Adds a small header dict so
    future readers know the recipe parameters that produced it."""
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "n": len(instance_ids),
        "frozen_at": "2026-05-03",
        "recipe": {
            "difficulties": sorted(_FAST_DIFFICULTIES),
            "per_repo_cap": 8,
            "feature_ratio_target": 0.30,
            "multi_file_ratio_target": 0.40,
        },
        "instance_ids": instance_ids,
    }
    p.write_text(json.dumps(payload, indent=2))


def read_subset(path: Path | str) -> list[str]:
    """Read a frozen subset list, returning instance_ids."""
    return list(json.loads(Path(path).read_text())["instance_ids"])
