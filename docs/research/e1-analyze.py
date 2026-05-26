#!/usr/bin/env python3
"""
E1 · Context-cliff characterization for luxe.

Pure read-only analysis: walks recorded taxonomy artifacts in
`/Users/michaeltimpe/Downloads/luxe/acceptance/v*_taxonomy/*.json`,
counts `CONTEXT_EXHAUSTED` and `EMPTY_PATCH_CONTEXT_EXHAUSTED` outcomes
across versions and reps, and cross-tabs with `failure_chain` heads,
`interventions`/`interventions_fired`, and `tier`.

Outputs (alongside this script):
  - e1-context-cliff-counts.csv : per-(version,rep,bench) × outcome counts
  - e1-context-cliff-report.md  : human-readable summary + tables

No writes to luxe. No model loaded. Run with: python3 e1-analyze.py
"""

from __future__ import annotations
import csv
import json
import os
import re
from collections import Counter, defaultdict
from glob import glob
from pathlib import Path

LUXE_ROOT = Path("/Users/michaeltimpe/Downloads/luxe")
ACCEPTANCE = LUXE_ROOT / "acceptance"
OUT_DIR = Path(__file__).resolve().parent

# Cliff outcomes we are characterizing.
CLIFF_OUTCOMES = {"CONTEXT_EXHAUSTED", "EMPTY_PATCH_CONTEXT_EXHAUSTED"}


def parse_filename(path: Path) -> tuple[str, str, str]:
    """Return (version, rep, bench). version like 'v111'; rep '1' default."""
    version = path.parent.name.replace("_taxonomy", "")
    name = path.stem
    # rep extraction: e.g. v111_n75_rep_2_full_stack_swebench → rep=2
    m = re.search(r"_rep_(\d+)_", name)
    rep = m.group(1) if m else "1"
    # bench inference
    if "bfcl" in name:
        bench = "bfcl"
    elif "swebench" in name or "swe_bench" in name:
        bench = "swebench"
    elif name == "aggregate":
        bench = "aggregate"
    else:
        bench = "unknown"
    return version, rep, bench


def normalize_row(row: dict) -> dict:
    """Coerce SWE-bench and BFCL row schemas to a common subset."""
    outcome = row.get("outcome")
    interventions = row.get("interventions") or row.get("interventions_fired") or []
    failure_chain = row.get("failure_chain") or []
    tier = row.get("tier")
    return {
        "outcome": outcome,
        "interventions": tuple(interventions) if isinstance(interventions, list) else (),
        "failure_chain": tuple(failure_chain) if isinstance(failure_chain, list) else (),
        "tier": tier,
        "instance_id": row.get("instance_id") or row.get("id"),
        "has_patch": row.get("has_patch"),
        "patch_len": row.get("patch_len"),
    }


def iter_runs():
    """Yield (version, rep, bench, file_path, rows[])."""
    for d in sorted(ACCEPTANCE.glob("v*_taxonomy")):
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text())
            except Exception as e:
                print(f"  skip {f}: {e}")
                continue
            version, rep, bench = parse_filename(f)
            # v17_taxonomy/aggregate.json is a nested structure; expand it
            if isinstance(data, dict) and "swebench" in data and "bfcl" in data:
                # aggregate of two benches; emit each as a virtual run
                for sub_bench in ("swebench", "bfcl"):
                    sub = data.get(sub_bench) or {}
                    rows = sub.get("rows")
                    if isinstance(rows, list):
                        yield version, rep, sub_bench, f, [normalize_row(r) for r in rows if isinstance(r, dict)]
                continue
            rows = None
            if isinstance(data, dict):
                rows = data.get("rows") or data.get("episodes")
            elif isinstance(data, list):
                rows = data
            if not isinstance(rows, list):
                continue
            yield version, rep, bench, f, [normalize_row(r) for r in rows if isinstance(r, dict)]


def version_sort_key(v: str) -> tuple:
    """Sort version strings like v17, v18, v19, v110, v1101 sensibly.
    Strategy: strip leading 'v', split on dots, but luxe encodes 'v1.10' as
    'v110' (no dot). So we read numeric prefix and treat 17/18/19/110/1101…
    as semver-ish: 17 -> (1,7), 18 -> (1,8), 19 -> (1,9), 110 -> (1,10),
    1101 -> (1,10,1), 1102 -> (1,10,2), 1105 -> (1,10,5), 111 -> (1,11)."""
    s = v.lstrip("v")
    # Try to split into major.minor[.patch]. Heuristic for luxe's tag style:
    if len(s) == 2:           # 17/18/19  → 1.{s[1]}
        return (int(s[0]), int(s[1]))
    if len(s) == 3:           # 110/111   → 1.{s[1:]}
        return (int(s[0]), int(s[1:]))
    if len(s) == 4:           # 1101..1105 → 1.10.{s[3]}
        return (int(s[0]), int(s[1:3]), int(s[3]))
    return (s,)


def main():
    per_run = []  # list of (version, rep, bench, total, outcome_counts, …)
    global_outcome = Counter()
    cliff_chain_heads = Counter()
    cliff_interventions = Counter()
    cliff_tiers = Counter()
    overall_tiers_swebench = Counter()
    files_seen = []

    for version, rep, bench, fpath, rows in iter_runs():
        files_seen.append(str(fpath))
        oc = Counter(r["outcome"] for r in rows)
        cliff_count = sum(oc.get(o, 0) for o in CLIFF_OUTCOMES)
        global_outcome.update(oc)
        for r in rows:
            if bench == "swebench" and r.get("tier"):
                overall_tiers_swebench[r["tier"]] += 1
            if r["outcome"] in CLIFF_OUTCOMES:
                # chain head = first FailureClass in chain (or None)
                head = r["failure_chain"][0] if r["failure_chain"] else "<empty>"
                cliff_chain_heads[head] += 1
                for iv in r["interventions"]:
                    cliff_interventions[iv] += 1
                if r.get("tier"):
                    cliff_tiers[r["tier"]] += 1
        per_run.append({
            "version": version,
            "rep": rep,
            "bench": bench,
            "total_rows": len(rows),
            "context_exhausted": oc.get("CONTEXT_EXHAUSTED", 0),
            "empty_patch_context_exhausted": oc.get("EMPTY_PATCH_CONTEXT_EXHAUSTED", 0),
            "cliff_total": cliff_count,
            "cliff_rate_pct": round(100.0 * cliff_count / len(rows), 2) if rows else 0.0,
            "file": str(fpath.relative_to(LUXE_ROOT)),
        })

    # Sort per_run by (bench, version_sort_key, rep)
    per_run.sort(key=lambda r: (r["bench"], version_sort_key(r["version"]), int(r["rep"])))

    # Write CSV
    csv_path = OUT_DIR / "e1-context-cliff-counts.csv"
    with csv_path.open("w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=[
            "bench", "version", "rep", "total_rows",
            "context_exhausted", "empty_patch_context_exhausted",
            "cliff_total", "cliff_rate_pct", "file",
        ])
        w.writeheader()
        for r in per_run:
            w.writerow(r)

    # Aggregate by (bench, version) over reps
    by_bv = defaultdict(lambda: {"runs": 0, "total_rows": 0, "ctx": 0, "empty_ctx": 0, "cliff": 0})
    for r in per_run:
        k = (r["bench"], r["version"])
        by_bv[k]["runs"] += 1
        by_bv[k]["total_rows"] += r["total_rows"]
        by_bv[k]["ctx"] += r["context_exhausted"]
        by_bv[k]["empty_ctx"] += r["empty_patch_context_exhausted"]
        by_bv[k]["cliff"] += r["cliff_total"]

    # Write markdown report
    md = []
    md.append("# E1 · Context-cliff characterization (luxe)\n")
    md.append("Read-only analysis of recorded taxonomy artifacts under\n"
              "`luxe/acceptance/v*_taxonomy/*.json`. No new runs, no model load,\n"
              "no writes to the luxe tree.\n")
    md.append("**Outcomes counted as 'the cliff':** "
              "`CONTEXT_EXHAUSTED` (backend 400 on prompt size) and "
              "`EMPTY_PATCH_CONTEXT_EXHAUSTED` (SWE-bench-specific tier: oMLX 400 prompt size).\n")
    md.append("**Source:** `src/luxe/agents/outcomes.py:55,97,258-259` defines both classes; "
              "they appear in `outcome` rows of the taxonomy artifacts.\n")

    md.append(f"## 1. Coverage\n")
    md.append(f"- Files analyzed: **{len(per_run)}** rows across "
              f"**{sum(r['total_rows'] for r in per_run)}** task instances.\n")
    md.append(f"- Benches present: " +
              ", ".join(sorted({r['bench'] for r in per_run})) + "\n")
    md.append(f"- Versions present (sorted): " +
              ", ".join(sorted({r['version'] for r in per_run}, key=version_sort_key)) + "\n")

    md.append("## 2. Per-run cliff incidence (CSV mirror)\n")
    md.append("| bench | version | rep | n | CONTEXT_EXHAUSTED | EMPTY_PATCH_CE | cliff total | cliff % |")
    md.append("|---|---|---|---|---|---|---|---|")
    for r in per_run:
        md.append(f"| {r['bench']} | {r['version']} | {r['rep']} | {r['total_rows']} | "
                  f"{r['context_exhausted']} | {r['empty_patch_context_exhausted']} | "
                  f"{r['cliff_total']} | {r['cliff_rate_pct']}% |")

    md.append("\n## 3. Per-(bench, version) aggregate over reps\n")
    md.append("| bench | version | runs | rows total | CE total | EMPTY_PATCH_CE total | cliff total | cliff %* |")
    md.append("|---|---|---|---|---|---|---|---|")
    for (bench, version), d in sorted(by_bv.items(), key=lambda kv: (kv[0][0], version_sort_key(kv[0][1]))):
        rate = round(100.0 * d["cliff"] / d["total_rows"], 2) if d["total_rows"] else 0.0
        md.append(f"| {bench} | {version} | {d['runs']} | {d['total_rows']} | "
                  f"{d['ctx']} | {d['empty_ctx']} | {d['cliff']} | {rate}% |")
    md.append("\n*cliff % = cliff total / rows total across reps.*\n")

    md.append("## 4. Failure-chain heads accompanying cliff outcomes (all benches, all versions)\n")
    if not cliff_chain_heads:
        md.append("_No cliff outcomes recorded in any artifact (no rows with "
                  "`CONTEXT_EXHAUSTED`/`EMPTY_PATCH_CONTEXT_EXHAUSTED`)._\n")
    else:
        md.append("| failure_chain head | count |")
        md.append("|---|---|")
        for k, v in cliff_chain_heads.most_common():
            md.append(f"| `{k}` | {v} |")

    md.append("\n## 5. Interventions fired on cliff-outcome rows (all benches, all versions)\n")
    if not cliff_interventions:
        md.append("_None recorded._\n")
    else:
        md.append("| intervention | count |")
        md.append("|---|---|")
        for k, v in cliff_interventions.most_common():
            md.append(f"| `{k}` | {v} |")

    md.append("\n## 6. Tier distribution of cliff outcomes (SWE-bench only)\n")
    if not cliff_tiers:
        md.append("_No cliff outcomes have a `tier` field (or none recorded)._\n")
    else:
        md.append("| tier | cliff rows | total SWE-bench rows (this tier across runs) | cliff share of tier |")
        md.append("|---|---|---|---|")
        for tier, n_cliff in cliff_tiers.most_common():
            n_total_tier = overall_tiers_swebench.get(tier, 0)
            share = round(100.0 * n_cliff / n_total_tier, 2) if n_total_tier else 0.0
            md.append(f"| `{tier}` | {n_cliff} | {n_total_tier} | {share}% |")

    md.append("\n## 7. Global outcome distribution (sanity check)\n")
    md.append("| outcome | count |")
    md.append("|---|---|")
    for k, v in global_outcome.most_common():
        md.append(f"| `{k}` | {v} |")

    md.append("\n## 8. What's *not* in this data (instrumentation gap)\n")
    md.append(
        "- `peak_context_pressure` is tracked **per run in `AgentResult`** "
        "(`src/luxe/agents/loop.py:50,680`) but is **not persisted into the "
        "taxonomy row schema** for SWE-bench (`instance_id`, `tier`, "
        "`has_patch`, `patch_len`, `outcome`, `interventions`, `failure_chain`, "
        "`gold_target_files`, `first_correct_file_touch_step`, "
        "`correct_touch_*`, `first_write_locus_correct`, `write_locus_*`, "
        "`gold_files_*`, `prior_patch_len`, `patch_len_delta`) or BFCL row "
        "schema (`id`, `category`, `outcome`, `interventions_fired`, "
        "`failure_chain`).\n"
        "- Consequence: we can characterize **terminal cliff events** "
        "(outcome = `*CONTEXT_EXHAUSTED`) from recorded data, but the **full "
        "pressure distribution** (e.g., how many runs spent N steps above the "
        "70% compaction threshold without terminating) is not available "
        "without later instrumentation. Surfacing `peak_context_pressure` (and "
        "ideally a per-step pressure histogram) into the taxonomy row would "
        "fill this gap. **Not done in this session.**\n"
    )

    md.append("## 9. Method & ground rules\n")
    md.append(
        "- Read-only access to `/Users/michaeltimpe/Downloads/luxe/acceptance/`.\n"
        "- No model loaded, no benchmarks run, no edits to the luxe tree.\n"
        "- All outputs written under "
        "`/Users/michaeltimpe/Downloads/agentic-patterns-luxe-research/`.\n"
        "- Reproducer: `python3 e1-analyze.py` from this folder.\n"
        f"- Files inspected ({len(files_seen)}):\n"
    )
    for f in files_seen:
        md.append(f"  - `{f}`")

    report = "\n".join(md) + "\n"
    (OUT_DIR / "e1-context-cliff-report.md").write_text(report)

    # Console summary
    n_files = len(per_run)
    n_rows = sum(r["total_rows"] for r in per_run)
    total_cliff = sum(r["cliff_total"] for r in per_run)
    total_ce = sum(r["context_exhausted"] for r in per_run)
    total_emp = sum(r["empty_patch_context_exhausted"] for r in per_run)
    print(f"Analyzed {n_files} run artifacts ({n_rows} rows total)")
    print(f"  CONTEXT_EXHAUSTED rows: {total_ce}")
    print(f"  EMPTY_PATCH_CONTEXT_EXHAUSTED rows: {total_emp}")
    print(f"  Cliff total: {total_cliff} ({round(100*total_cliff/n_rows,3)}%)")
    print(f"  Distinct failure_chain heads on cliff rows: {len(cliff_chain_heads)}")
    print(f"  Distinct interventions on cliff rows: {len(cliff_interventions)}")
    print(f"\nWrote:")
    print(f"  {OUT_DIR / 'e1-context-cliff-counts.csv'}")
    print(f"  {OUT_DIR / 'e1-context-cliff-report.md'}")


if __name__ == "__main__":
    main()
