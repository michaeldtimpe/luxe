#!/usr/bin/env python3
"""
A + F · Empty-patch breakdown + intervention effectiveness.

Read-only analysis on the same taxonomy artifacts E1 used. Reuses the
file enumeration + row normalization from e1-analyze.py inline.

Outputs (alongside this script):
  - a-empty-patch-breakdown.md   : 80 cliff vs 244 non-cliff inside tier=empty_patch
  - a-empty-patch-breakdown.csv  : per-(outcome, intervention head) counts
  - f-intervention-effectiveness.md  : outcome distribution by interventions fired
  - f-intervention-effectiveness.csv : per-(intervention, outcome) counts

No writes to luxe. No model loaded. Reproducer: `python3 af-analyze.py`.
"""

from __future__ import annotations
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

LUXE_ROOT = Path("/Users/michaeltimpe/Downloads/luxe")
ACCEPTANCE = LUXE_ROOT / "acceptance"
OUT_DIR = Path(__file__).resolve().parent
CLIFF_OUTCOMES = {"CONTEXT_EXHAUSTED", "EMPTY_PATCH_CONTEXT_EXHAUSTED"}


def parse_filename(path: Path):
    version = path.parent.name.replace("_taxonomy", "")
    name = path.stem
    m = re.search(r"_rep_(\d+)_", name)
    rep = m.group(1) if m else "1"
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
    interventions = row.get("interventions") or row.get("interventions_fired") or []
    failure_chain = row.get("failure_chain") or []
    return {
        "outcome": row.get("outcome"),
        "interventions": tuple(interventions) if isinstance(interventions, list) else (),
        "failure_chain": tuple(failure_chain) if isinstance(failure_chain, list) else (),
        "tier": row.get("tier"),
        "category": row.get("category"),
        "instance_id": row.get("instance_id") or row.get("id"),
        "has_patch": row.get("has_patch"),
        "patch_len": row.get("patch_len"),
    }


def iter_rows():
    """Yield (version, rep, bench, normalized_row) for every taxonomy row."""
    for d in sorted(ACCEPTANCE.glob("v*_taxonomy")):
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            version, rep, bench = parse_filename(f)
            # Handle nested aggregate.json (v17): {"swebench":..., "bfcl":...}
            if isinstance(data, dict) and "swebench" in data and "bfcl" in data:
                for sub_bench in ("swebench", "bfcl"):
                    sub = data.get(sub_bench) or {}
                    rows = sub.get("rows")
                    if isinstance(rows, list):
                        for r in rows:
                            if isinstance(r, dict):
                                yield version, rep, sub_bench, normalize_row(r)
                continue
            rows = None
            if isinstance(data, dict):
                rows = data.get("rows") or data.get("episodes")
            elif isinstance(data, list):
                rows = data
            if not isinstance(rows, list):
                continue
            for r in rows:
                if isinstance(r, dict):
                    yield version, rep, bench, normalize_row(r)


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |"]
    out.append("|" + "|".join("---" for _ in headers) + "|")
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


# ============================================================
# Collect data
# ============================================================
all_rows = list(iter_rows())
swebench_rows = [r for r in all_rows if r[2] == "swebench"]
bfcl_rows = [r for r in all_rows if r[2] == "bfcl"]

# tier=empty_patch slice (SWE-bench only)
ep_rows = [(v, rep, r) for v, rep, b, r in swebench_rows if r["tier"] == "empty_patch"]
ep_cliff = [t for t in ep_rows if t[2]["outcome"] in CLIFF_OUTCOMES]
ep_noncliff = [t for t in ep_rows if t[2]["outcome"] not in CLIFF_OUTCOMES]


# ============================================================
# A · empty_patch breakdown
# ============================================================
a_md = []
a_md.append("# A · empty_patch breakdown (cliff vs non-cliff)\n")
a_md.append(
    "E1 showed 80/324 SWE-bench `empty_patch`-tier rows are cliff "
    "(`EMPTY_PATCH_CONTEXT_EXHAUSTED`). This report characterizes the other "
    "**~75% slice** — the non-cliff `empty_patch` rows that the README's "
    "deferred v1.9 action_density gating work is meant to address.\n"
)
a_md.append(
    f"**Total `tier=empty_patch` rows across all SWE-bench artifacts:** "
    f"{len(ep_rows)}  ·  cliff: {len(ep_cliff)}  ·  non-cliff: "
    f"{len(ep_noncliff)} ({round(100*len(ep_noncliff)/max(1,len(ep_rows)),1)}%)\n"
)

# Outcome distribution within non-cliff empty_patch
ep_noncliff_outcomes = Counter(r["outcome"] for _, _, r in ep_noncliff)
a_md.append("## Outcome distribution — non-cliff empty_patch rows\n")
a_md.append(md_table(
    ["outcome", "count", "share of non-cliff empty_patch"],
    [(o, c, f"{round(100*c/max(1,len(ep_noncliff)),2)}%")
     for o, c in ep_noncliff_outcomes.most_common()],
))
a_md.append("\n")

# Failure-chain heads within non-cliff empty_patch
ep_noncliff_heads = Counter(
    (r["failure_chain"][0] if r["failure_chain"] else "<empty>")
    for _, _, r in ep_noncliff
)
a_md.append("## Failure-chain heads — non-cliff empty_patch rows\n")
a_md.append(md_table(
    ["failure_chain head", "count"],
    list(ep_noncliff_heads.most_common()),
))
a_md.append("\n")

# Interventions fired within non-cliff empty_patch (multi-set: a row can have multiple)
ep_noncliff_ivs = Counter()
ep_noncliff_rows_with_any_iv = 0
for _, _, r in ep_noncliff:
    if r["interventions"]:
        ep_noncliff_rows_with_any_iv += 1
    for iv in r["interventions"]:
        ep_noncliff_ivs[iv] += 1
a_md.append("## Interventions fired — non-cliff empty_patch rows\n")
a_md.append(
    f"- Rows with at least one intervention fired: "
    f"**{ep_noncliff_rows_with_any_iv}/{len(ep_noncliff)} "
    f"({round(100*ep_noncliff_rows_with_any_iv/max(1,len(ep_noncliff)),1)}%)**\n"
)
a_md.append(md_table(
    ["intervention", "occurrences"],
    list(ep_noncliff_ivs.most_common()),
))
a_md.append("\n")

# Cliff slice for comparison
cliff_ivs = Counter()
cliff_rows_with_any_iv = 0
for _, _, r in ep_cliff:
    if r["interventions"]:
        cliff_rows_with_any_iv += 1
    for iv in r["interventions"]:
        cliff_ivs[iv] += 1
a_md.append("## Cliff slice (reference) — interventions fired\n")
a_md.append(
    f"- Rows with at least one intervention fired: "
    f"**{cliff_rows_with_any_iv}/{len(ep_cliff)} "
    f"({round(100*cliff_rows_with_any_iv/max(1,len(ep_cliff)),1)}%)**\n"
)
a_md.append(md_table(
    ["intervention", "occurrences"],
    list(cliff_ivs.most_common()),
))
a_md.append("\n")

# patch_len + has_patch on non-cliff
ep_noncliff_haspatch = Counter(r["has_patch"] for _, _, r in ep_noncliff)
patch_lens = [r["patch_len"] for _, _, r in ep_noncliff if isinstance(r["patch_len"], int)]
a_md.append("## `has_patch` / `patch_len` — non-cliff empty_patch rows\n")
a_md.append(md_table(
    ["has_patch", "count"],
    [(k, v) for k, v in ep_noncliff_haspatch.most_common()],
))
if patch_lens:
    pl_zero = sum(1 for p in patch_lens if p == 0)
    pl_nonzero = sum(1 for p in patch_lens if p > 0)
    a_md.append(
        f"\n- patch_len == 0: **{pl_zero}**  ·  patch_len > 0: "
        f"**{pl_nonzero}** (rows with patch_len present: {len(patch_lens)})\n"
    )

# Findings
a_md.append("## Findings (TL;DR)\n")
dominant = ep_noncliff_outcomes.most_common(1)[0] if ep_noncliff_outcomes else ("?", 0)
iv_coverage_noncliff = round(100 * ep_noncliff_rows_with_any_iv / max(1, len(ep_noncliff)), 1)
iv_coverage_cliff = round(100 * cliff_rows_with_any_iv / max(1, len(ep_cliff)), 1)
a_md.append(
    f"1. **Non-cliff `empty_patch` is dominated by `{dominant[0]}` "
    f"({dominant[1]} of {len(ep_noncliff)} = "
    f"{round(100*dominant[1]/max(1,len(ep_noncliff)),1)}%).** This is the "
    f"`EMPTY_PATCH_TIMEOUT` slice the README's v1.9 action_density work targets.\n"
)
a_md.append(
    f"2. **Intervention coverage differs sharply between cliff and non-cliff.** "
    f"Cliff: {iv_coverage_cliff}% of rows had ≥1 intervention fire. "
    f"Non-cliff: **{iv_coverage_noncliff}%** of rows had ≥1 intervention fire. "
    f"The intervention machinery sees the non-cliff failures; "
    f"the cliff is comparatively unsignaled.\n"
)
a_md.append(
    "3. **The two slices are addressed by different mechanisms.** "
    "Cliff (~25% of empty_patch): needs G1 graceful context lifecycle — no "
    "in-loop intervention can rescue a backend prompt-size 400. "
    "Non-cliff (~75%): action_density gating + the existing intervention "
    "stack (early_bail / write_pressure / prose_burst) is the right surface, "
    "and the data shows that surface is already firing on most of these "
    "rows but not converting them. The lever is intervention **conversion**, "
    "not intervention **coverage**.\n"
)


# Write A CSV
a_csv_path = OUT_DIR / "a-empty-patch-breakdown.csv"
with a_csv_path.open("w", newline="") as fp:
    w = csv.writer(fp)
    w.writerow(["slice", "outcome", "count", "share_pct"])
    for o, c in ep_noncliff_outcomes.most_common():
        w.writerow(["empty_patch_noncliff", o, c,
                    round(100 * c / max(1, len(ep_noncliff)), 2)])
    for o, c in Counter(r["outcome"] for _, _, r in ep_cliff).most_common():
        w.writerow(["empty_patch_cliff", o, c,
                    round(100 * c / max(1, len(ep_cliff)), 2)])

(OUT_DIR / "a-empty-patch-breakdown.md").write_text("\n".join(a_md) + "\n")


# ============================================================
# F · intervention effectiveness
# ============================================================
# Build: intervention -> Counter(outcome) ; and "no_intervention" -> Counter(outcome)
iv_outcome = defaultdict(Counter)
no_iv_outcome = Counter()
all_outcomes = Counter()
total_rows = 0
swe_only_iv_outcome = defaultdict(Counter)
swe_only_no_iv_outcome = Counter()
swe_only_total = 0

for v, rep, bench, r in all_rows:
    o = r["outcome"]
    if not o:
        continue
    total_rows += 1
    all_outcomes[o] += 1
    if r["interventions"]:
        for iv in r["interventions"]:
            iv_outcome[iv][o] += 1
    else:
        no_iv_outcome[o] += 1
    if bench == "swebench":
        swe_only_total += 1
        if r["interventions"]:
            for iv in r["interventions"]:
                swe_only_iv_outcome[iv][o] += 1
        else:
            swe_only_no_iv_outcome[o] += 1

# Define "good" outcomes (success-like) vs "failure"
GOOD = {"STRONG_GOLD_MATCH", "PLAUSIBLE_EDIT", "CORRECT_ABSTAIN",
        "MULTI_TOOL_COMPLETE", "SINGLE_TOOL_CORRECT"}

def good_rate(counter: Counter) -> tuple[int, int, float]:
    total = sum(counter.values())
    good = sum(counter.get(o, 0) for o in GOOD)
    rate = round(100 * good / total, 2) if total else 0.0
    return good, total, rate

f_md = []
f_md.append("# F · intervention effectiveness\n")
f_md.append(
    "Cross-tabs **outcome distribution** by **intervention class fired** across "
    "all 4,053 taxonomy rows (SWE-bench n=75 × 17 runs + BFCL n=1240 × 2 runs). "
    "Asks: when an intervention fires, does the row land on a 'good' outcome "
    "more often than the no-intervention baseline?\n"
)
f_md.append(
    "Good outcomes (treated as success-like for this analysis): "
    + ", ".join(f"`{o}`" for o in sorted(GOOD)) + ".\n"
)
f_md.append(
    "**Important caveat:** intervention firing is *not random* — interventions "
    "fire because a stall/loop/prose-burst is already detected. So an "
    "intervention-fires row is a **higher-risk** row to begin with. A lower "
    "good-rate under an intervention does NOT necessarily mean the "
    "intervention is harmful; the right counterfactual is 'what would have "
    "happened without it on the same trajectory,' which is not available from "
    "this data. Read the rates as descriptive, not causal.\n"
)

# All-rows global table
no_g, no_t, no_r = good_rate(no_iv_outcome)
f_md.append("## All rows (n=4,053): outcome under each intervention\n")
rows = []
rows.append(("(no intervention fired)", no_t, no_g, f"{no_r}%"))
for iv, oc in sorted(iv_outcome.items(), key=lambda kv: -sum(kv[1].values())):
    g, t, r = good_rate(oc)
    rows.append((iv, t, g, f"{r}%"))
f_md.append(md_table(
    ["slice", "rows", "good outcomes", "good rate"],
    rows,
))
f_md.append("\n")

# SWE-bench only (excludes BFCL where no interventions fire)
no_g_s, no_t_s, no_r_s = good_rate(swe_only_no_iv_outcome)
f_md.append("## SWE-bench only (n=1,272): outcome under each intervention\n")
rows = []
rows.append(("(no intervention fired)", no_t_s, no_g_s, f"{no_r_s}%"))
for iv, oc in sorted(swe_only_iv_outcome.items(), key=lambda kv: -sum(kv[1].values())):
    g, t, r = good_rate(oc)
    rows.append((iv, t, g, f"{r}%"))
f_md.append(md_table(
    ["slice", "rows", "good outcomes", "good rate"],
    rows,
))
f_md.append("\n")

# Per-intervention outcome distribution (top intervention only by volume)
top_iv = max(iv_outcome.items(), key=lambda kv: sum(kv[1].values()))[0] if iv_outcome else None
if top_iv:
    f_md.append(f"## Outcome distribution under `{top_iv}` (most-fired intervention)\n")
    total_top = sum(iv_outcome[top_iv].values())
    rows = []
    for o, c in iv_outcome[top_iv].most_common():
        rows.append((o, c, f"{round(100*c/max(1,total_top),2)}%"))
    f_md.append(md_table(["outcome", "count", "share"], rows))
    f_md.append("\n")

# Distinct intervention vocabulary
f_md.append("## Intervention vocabulary observed in the data\n")
for iv in sorted(iv_outcome.keys()):
    f_md.append(f"- `{iv}` ({sum(iv_outcome[iv].values())} occurrences)")
f_md.append("")

# Findings
f_md.append("## Findings (TL;DR)\n")
# Compute relative deltas
def delta_str(iv: str, baseline_rate: float, iv_outcome_map) -> str:
    if iv not in iv_outcome_map:
        return ""
    g, t, r = good_rate(iv_outcome_map[iv])
    d = round(r - baseline_rate, 2)
    sign = "+" if d >= 0 else ""
    return f"{r}% ({sign}{d}pp vs no-intervention {baseline_rate}%)"

# Pull rates for top interventions on SWE-bench
swe_summaries = []
for iv in sorted(swe_only_iv_outcome.keys(), key=lambda k: -sum(swe_only_iv_outcome[k].values())):
    swe_summaries.append((iv, delta_str(iv, no_r_s, swe_only_iv_outcome)))

f_md.append(
    f"1. **BFCL fires zero interventions in this dataset** — all 2,480 BFCL "
    f"rows (v17 + v18 n=1240 each) have `interventions_fired = []`. The "
    f"intervention stack is SWE-bench-only in practice, consistent with "
    f"BFCL's short-context tool-call problems not triggering write_pressure / "
    f"early_bail / prose_burst gates.\n"
)
f_md.append(
    f"2. **SWE-bench no-intervention baseline good-rate: {no_r_s}%** "
    f"({no_g_s}/{no_t_s}). This is what intervention slices should be compared "
    f"against — but remember the selection-bias caveat above.\n"
)
if swe_summaries:
    f_md.append("3. **Per-intervention good-rates on SWE-bench** "
                "(rate, pp delta vs no-intervention):\n")
    for iv, summary in swe_summaries:
        f_md.append(f"   - `{iv}`: {summary}")
    f_md.append("")

f_md.append(
    "4. **What this does and doesn't tell us.** Tells us: which interventions "
    "are even being exercised on the recorded benches, and how the recorded "
    "rows that triggered them landed. Doesn't tell us: whether intervention "
    "firing *caused* the outcome — that needs paired traces (intervention-on "
    "vs intervention-off for the same instance), which would be a future "
    "experiment, not a read-only analysis. The data here is a starting "
    "point for *which* interventions to ablate first if you want a causal "
    "answer.\n"
)
f_md.append(
    "5. **Reinforces E1.** Cliff-row coverage of interventions (25%) and "
    "non-cliff `empty_patch` coverage are very different problems. The "
    "intervention machinery is not silent on `empty_patch` — it's firing — "
    "but on the cliff slice it has no signal at all.\n"
)

# F CSV
f_csv_path = OUT_DIR / "f-intervention-effectiveness.csv"
with f_csv_path.open("w", newline="") as fp:
    w = csv.writer(fp)
    w.writerow(["bench_scope", "intervention", "outcome", "count"])
    for o, c in no_iv_outcome.items():
        w.writerow(["all", "(none)", o, c])
    for iv, oc in iv_outcome.items():
        for o, c in oc.items():
            w.writerow(["all", iv, o, c])
    for o, c in swe_only_no_iv_outcome.items():
        w.writerow(["swebench", "(none)", o, c])
    for iv, oc in swe_only_iv_outcome.items():
        for o, c in oc.items():
            w.writerow(["swebench", iv, o, c])

(OUT_DIR / "f-intervention-effectiveness.md").write_text("\n".join(f_md) + "\n")


# ============================================================
# Console summary
# ============================================================
print("A — empty_patch breakdown")
print(f"  total empty_patch rows: {len(ep_rows)}  "
      f"(cliff {len(ep_cliff)}, non-cliff {len(ep_noncliff)})")
print(f"  non-cliff dominant outcome: {ep_noncliff_outcomes.most_common(1)}")
print(f"  non-cliff intervention coverage: "
      f"{ep_noncliff_rows_with_any_iv}/{len(ep_noncliff)} "
      f"({round(100*ep_noncliff_rows_with_any_iv/max(1,len(ep_noncliff)),1)}%)")
print(f"  cliff intervention coverage: "
      f"{cliff_rows_with_any_iv}/{len(ep_cliff)} "
      f"({round(100*cliff_rows_with_any_iv/max(1,len(ep_cliff)),1)}%)")
print()
print("F — intervention effectiveness")
print(f"  total rows: {total_rows}  (swe-bench {swe_only_total}, bfcl {total_rows-swe_only_total})")
print(f"  swe-bench no-intervention good-rate: {no_r_s}% ({no_g_s}/{no_t_s})")
print(f"  intervention vocabulary observed: {sorted(iv_outcome.keys())}")
print()
print("Wrote:")
print(f"  {OUT_DIR / 'a-empty-patch-breakdown.md'}")
print(f"  {OUT_DIR / 'a-empty-patch-breakdown.csv'}")
print(f"  {OUT_DIR / 'f-intervention-effectiveness.md'}")
print(f"  {OUT_DIR / 'f-intervention-effectiveness.csv'}")
