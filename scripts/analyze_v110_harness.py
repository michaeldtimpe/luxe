"""Post-Docker-harness analyzer for v1.10 (one-shot; produces the
numbers W1-follow-up needs to drop into RESUME.md).

Reads:
    acceptance/swebench/post_specdd_v110_n75/rep_1/harness/harness_summary.json
    acceptance/v110_taxonomy/v110_n75_full_stack_swebench.json
    acceptance/swebench/post_specdd_v19_n75/rep_1/harness/harness_summary.json
    acceptance/v19_taxonomy/full_stack_swebench_n75.json

Emits to stdout:
    - patched and overall denominators (kept visually separated)
    - per-tier resolution table (empty_patch row omitted by design)
    - thesis check A: matplotlib-14623 resolved status
    - thesis check B: recovery-gain (of 7 v19-empty → v110 non-empty, how many resolved)
    - net Docker delta vs v19 (34 resolves baseline)
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

V110_HARNESS = REPO / "acceptance/swebench/post_specdd_v110_n75/rep_1/harness/harness_summary.json"
V110_TAX = REPO / "acceptance/v110_taxonomy/v110_n75_full_stack_swebench.json"
V19_HARNESS = REPO / "acceptance/swebench/post_specdd_v19_n75/rep_1/harness/harness_summary.json"
V19_TAX = REPO / "acceptance/v19_taxonomy/full_stack_swebench_n75.json"

# The 7 v19-full-stack empties that became non-empty in v110 (from the audit cross-compare)
RECOVERIES = [
    "astropy__astropy-14096",
    "matplotlib__matplotlib-20676",
    "matplotlib__matplotlib-20826",
    "psf__requests-5414",
    "pydata__xarray-3095",
    "pylint-dev__pylint-4604",
    "sphinx-doc__sphinx-10323",
]

# The 2 new regressions v19-non-empty → v110-empty
REGRESSIONS = ["sympy__sympy-13031", "matplotlib__matplotlib-14623"]


def load_taxonomy(path: Path) -> dict[str, dict]:
    with path.open() as f:
        return {r["instance_id"]: r for r in json.load(f)["rows"]}


def load_harness(path: Path) -> dict[str, dict]:
    with path.open() as f:
        return json.load(f)["instances"]


def main() -> None:
    if not V110_HARNESS.exists():
        raise SystemExit(f"missing: {V110_HARNESS} — run the Docker harness first")

    v110_tax = load_taxonomy(V110_TAX)
    v110_h = load_harness(V110_HARNESS)
    v19_h = load_harness(V19_HARNESS)
    v19_tax = load_taxonomy(V19_TAX)

    # ---- A. denominators
    n_total = len(v110_tax)
    patched_ids = {iid for iid, r in v110_tax.items() if r["has_patch"]}
    n_patched = len(patched_ids)
    n_resolved = sum(1 for iid in patched_ids if v110_h.get(iid, {}).get("resolved"))
    v19_resolved = sum(1 for v in v19_h.values() if v.get("resolved"))
    v19_patched = len(v19_h)
    print(f"=== W1 follow-up — Docker harness analysis (v1.10 vs v1.9) ===\n")
    print(f"v1.10 Docker harness: {n_resolved} / {n_patched} patched ({100*n_resolved/n_patched:.1f}%)")
    print(f"v1.10 overall:        {n_resolved} / {n_total} total instances ({100*n_resolved/n_total:.1f}%)")
    print(f"v1.9  Docker harness: {v19_resolved} / {v19_patched} patched ({100*v19_resolved/v19_patched:.1f}%)")
    print(f"v1.9  overall:        {v19_resolved} / 75 total instances ({100*v19_resolved/75:.1f}%)")
    print(f"\nDelta resolves: {n_resolved - v19_resolved:+d} (v1.10 {n_resolved} vs v1.9 {v19_resolved})")

    # ---- B. per-tier resolution (omit empty_patch row)
    print("\n--- per-tier resolution (intersected with has_patch only) ---")
    by_tier_total: Counter[str] = Counter()
    by_tier_resolved: Counter[str] = Counter()
    for iid, r in v110_tax.items():
        if not r["has_patch"]:
            continue
        t = r["tier"]
        by_tier_total[t] += 1
        if v110_h.get(iid, {}).get("resolved"):
            by_tier_resolved[t] += 1
    print(f"{'tier':<20} {'n_with_patch':>13} {'n_resolved':>11} {'rate':>8}")
    for tier in ("strong", "plausible", "wrong_target", "wrong_location", "new_file_in_diff"):
        tot = by_tier_total.get(tier, 0)
        res = by_tier_resolved.get(tier, 0)
        rate = f"{100*res/tot:.1f}%" if tot else "n/a"
        print(f"{tier:<20} {tot:>13} {res:>11} {rate:>8}")
    print(f"{'empty_patch':<20} {'(no patch)':>13} {'—':>11} {'—':>8}   ← omitted by design")

    # ---- C. thesis checks
    print("\n--- thesis A: regression-loss (matplotlib-14623 expected resolved=False) ---")
    mpl = v110_h.get("matplotlib__matplotlib-14623", {})
    print(f"  matplotlib-14623 in v1.10 harness: resolved={mpl.get('resolved', '<missing>')}, error={mpl.get('error', '')!r}")
    mpl_v19 = v19_h.get("matplotlib__matplotlib-14623", {})
    print(f"  matplotlib-14623 in v1.9  harness: resolved={mpl_v19.get('resolved', '<missing>')}")
    print(f"  Surrender confirmed: {mpl_v19.get('resolved') and not mpl.get('resolved')}")

    print("\n--- thesis B: recovery-gain (of 7 v19-empty → v110 non-empty, how many resolve?) ---")
    recovered_resolved = []
    recovered_unresolved = []
    for iid in RECOVERIES:
        rec = v110_h.get(iid, {})
        if rec.get("resolved"):
            recovered_resolved.append(iid)
        else:
            recovered_unresolved.append(iid)
    print(f"  Resolved   ({len(recovered_resolved)}/{len(RECOVERIES)}):")
    for iid in recovered_resolved:
        tier = v110_tax[iid]["tier"]
        print(f"    + {iid} (tier={tier})")
    print(f"  Unresolved ({len(recovered_unresolved)}/{len(RECOVERIES)}):")
    for iid in recovered_unresolved:
        tier = v110_tax[iid]["tier"]
        print(f"    - {iid} (tier={tier})")

    # ---- C.5 locus × Docker resolution cross-tab (v1.10.1 substrate for v1.11)
    # Bridges trajectory reconnaissance (did the model touch the gold target
    # file? before or after intervention?) to Docker resolution probability.
    # Expectation: instances where the gold file was touched BEFORE the
    # first write resolve at a much higher rate than instances where it
    # was touched AFTER intervention or NEVER.
    print("\n--- locus × Docker resolution cross-tab (v1.11 substrate) ---")
    has_locus = any("correct_touch_relative_to_intervention" in r for r in v110_tax.values())
    if not has_locus:
        print("  (locus fields missing from v110_tax — run scripts/compare_v110.py first)")
    else:
        from collections import defaultdict
        buckets: dict[str, dict[str, int]] = defaultdict(
            lambda: {"n": 0, "resolved": 0})
        for iid, r in v110_tax.items():
            if not r.get("has_patch"):
                continue
            rel = r.get("correct_touch_relative_to_intervention", "none")
            bf = r.get("correct_touch_before_first_write", False)
            # Three buckets: "before write & before intervention" (best),
            # "after intervention" (locus-failure signature), "never touched"
            if rel == "none":
                bucket = "never_touched_gold"
            elif rel == "after":
                bucket = "touched_after_intervention"
            elif bf:
                bucket = "touched_before_write"
            else:
                bucket = "touched_before_intervention_but_after_write"
            buckets[bucket]["n"] += 1
            if v110_h.get(iid, {}).get("resolved"):
                buckets[bucket]["resolved"] += 1
        print(f"  {'bucket':<45} {'n':>5} {'resolved':>10} {'rate':>8}")
        for bucket, stats in sorted(buckets.items(),
                                    key=lambda kv: -kv[1]["resolved"] / max(1, kv[1]["n"])):
            n = stats["n"]
            r = stats["resolved"]
            rate = f"{100*r/n:.1f}%" if n else "n/a"
            print(f"  {bucket:<45} {n:>5} {r:>10} {rate:>8}")

    # ---- D. silent same-tier Docker demotion class (v1.10.1)
    # Surfaces sphinx-10673 archetype: inspector tier unchanged across
    # cycles, prior cycle Docker-resolved, current cycle Docker-failed.
    # The inspector taxonomy is structurally blind to this — patch
    # shrinkage that preserves wrong-locus characteristics doesn't move
    # the tier but can flip an alternative-solution Docker pass to fail.
    print("\n--- silent_demotion: same_tier_docker_demotion class ---")
    silent_demotions: list[dict] = []
    for iid, v110_row in v110_tax.items():
        v19_row = v19_tax.get(iid)
        if v19_row is None:
            continue
        if v110_row.get("tier") != v19_row.get("tier"):
            continue
        v19_resolved_iid = v19_h.get(iid, {}).get("resolved", False)
        v110_resolved_iid = v110_h.get(iid, {}).get("resolved", False)
        if not (v19_resolved_iid and not v110_resolved_iid):
            continue
        delta = (v110_row.get("patch_len") or 0) - (v19_row.get("patch_len") or 0)
        silent_demotions.append({
            "instance_id": iid,
            "tier": v110_row.get("tier"),
            "v19_patch_len": v19_row.get("patch_len"),
            "v110_patch_len": v110_row.get("patch_len"),
            "patch_len_delta": delta,
        })
    if silent_demotions:
        print(f"  {len(silent_demotions)} instance(s) with same-tier Docker demotion:")
        for s in sorted(silent_demotions, key=lambda x: x["patch_len_delta"]):
            print(f"    - {s['instance_id']}  tier={s['tier']}  "
                  f"patch_len {s['v19_patch_len']} -> {s['v110_patch_len']} "
                  f"(Δ={s['patch_len_delta']:+d})")
    else:
        print("  (none — no instances had same_tier Docker demotion)")

    # ---- E. verdict
    print("\n--- verdict ---")
    # Account for matplotlib-14623 surrender (was resolved in v19, now unresolved in v110)
    # plus any other v19-resolved instances that regressed in v110
    v19_resolved_ids = {iid for iid, v in v19_h.items() if v.get("resolved")}
    v110_resolved_ids = {iid for iid, v in v110_h.items() if v.get("resolved")}
    common = v19_resolved_ids & v110_resolved_ids
    surrendered = v19_resolved_ids - v110_resolved_ids
    gained = v110_resolved_ids - v19_resolved_ids
    print(f"  Kept resolves:        {len(common)}")
    print(f"  Surrendered resolves: {len(surrendered)} -> {sorted(surrendered)}")
    print(f"  New resolves:         {len(gained)} -> {sorted(gained)}")
    net = n_resolved - v19_resolved
    if net > 0:
        verdict = "Docker-WIN"
    elif net == 0:
        verdict = "Docker-WASH"
    else:
        verdict = "Docker-LOSS"
    print(f"  Net delta: {net:+d} resolves => v1.10 ships as {verdict}")


if __name__ == "__main__":
    main()
