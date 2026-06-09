#!/usr/bin/env python
"""Analyze the Track B chunk-conclude A/B (scripts/out/chunk_conclude_ab.csv).

Experiment plan: ~/.claude/plans/elegant-painting-mccarthy.md

Per the review, the unit of analysis is the CHUNK (not the chunk-rep): aggregate the
R reps per chunk FIRST (majority vote), then pair A0 vs each arm per-chunk
(McNemar) — running raw stats over the 41×3 reps would inflate DoF via within-chunk
correlation. Reports, per arm:
  - conclude-rate (chunk's OWN output had the report header/JSON — the prevention win)
  - lost-rate (no header AND not heuristic-salvageable — truly lost)
  - instability (chunks whose concluded outcome flips across reps — the ramble attractor)
  - McNemar paired vs A0 on concluded (b/c discordant + sign-test p)
  - findings retained on the intersection (chunks both concluded) — suppression guardrail
Run anytime; tolerates a partially-complete CSV (resumable run in progress).
"""
from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import median

CSV = Path(__file__).resolve().parent / "out" / "chunk_conclude_ab.csv"


def _binom_two_sided_p(b: int, c: int) -> float:
    """Exact sign-test p for McNemar's discordant pairs (n=b+c, p=0.5)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    cum = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * cum)


def main() -> None:
    rows = list(csv.DictReader(CSV.open()))
    # cells[(repo,arm,chunk)] = list of rep dicts
    cells: dict[tuple, list[dict]] = defaultdict(list)
    arms_seen, repos_seen = set(), set()
    for r in rows:
        cells[(r["repo"], r["arm"], int(r["chunk_index"]))].append(r)
        arms_seen.add(r["arm"]); repos_seen.add(r["repo"])

    def concluded(rep) -> bool:
        return rep["outcome"] == "concluded"

    def usable(rep) -> bool:
        return rep["outcome"] in ("concluded", "salvageable")

    # per-chunk aggregate (majority of reps)
    def agg(repo, arm):
        # returns {chunk: {"concl":bool,"usable":bool,"unstable":bool,"findings":float,"n":int}}
        out = {}
        for (rp, a, ch), reps in cells.items():
            if rp != repo or a != arm:
                continue
            n = len(reps)
            cyes = sum(concluded(x) for x in reps)
            uyes = sum(usable(x) for x in reps)
            out[ch] = {
                "concl": cyes * 2 >= n,            # majority concluded (own header/json)
                "usable": uyes * 2 >= n,           # majority recoverable
                "unstable": 0 < cyes < n,          # outcome flipped across reps
                "findings": median(int(x["findings"]) for x in reps),
                "n": n,
            }
        return out

    repos = sorted(repos_seen)
    arms = [a for a in ["A0", "A1", "A2", "A3", "A4"] if a in arms_seen]
    print(f"repos={repos}  arms={arms}\n")

    # pool decision repos (deluxe+neo-llm-bench) for the primary read; show controls separately
    decision = [r for r in repos if r in ("deluxe", "neo-llm-bench")]
    control = [r for r in repos if r not in decision]

    def pool(arm, repolist):
        m = {}
        for repo in repolist:
            for ch, v in agg(repo, arm).items():
                m[(repo, ch)] = v
        return m

    base = pool("A0", decision) if "A0" in arms else {}
    print(f"{'arm':4} {'chunks':>6} {'concl%':>7} {'lost%':>6} {'unstable%':>9} "
          f"{'McN b/c':>9} {'p':>6} {'find_ratio':>10}")
    for arm in arms:
        m = pool(arm, decision)
        nch = len(m)
        if not nch:
            continue
        concl = sum(v["concl"] for v in m.values())
        lost = sum(not v["usable"] for v in m.values())
        unstable = sum(v["unstable"] for v in m.values())
        line = f"{arm:4} {nch:>6} {100*concl/nch:>6.0f}% {100*lost/nch:>5.0f}% {100*unstable/nch:>8.0f}%"
        if arm != "A0" and base:
            common = [k for k in m if k in base]
            b = sum(1 for k in common if base[k]["concl"] and not m[k]["concl"])  # A0 only
            c = sum(1 for k in common if m[k]["concl"] and not base[k]["concl"])  # arm only
            p = _binom_two_sided_p(b, c)
            inter = [k for k in common if base[k]["usable"] and m[k]["usable"]]
            a0f = sum(base[k]["findings"] for k in inter) or 1
            armf = sum(m[k]["findings"] for k in inter)
            line += f" {f'{b}/{c}':>9} {p:>6.3f} {armf/a0f:>9.2f}x"
        else:
            line += f" {'—':>9} {'—':>6} {'—':>10}"
        print(line)

    # A0 noise band (per-rep gap-rate range) on decision repos — sanity check
    if "A0" in arms and decision:
        per_rep = defaultdict(lambda: [0, 0])  # rep -> [lost, total]
        for (rp, a, ch), reps in cells.items():
            if a != "A0" or rp not in decision:
                continue
            for x in reps:
                per_rep[int(x["rep"])][1] += 1
                if not usable(x):
                    per_rep[int(x["rep"])][0] += 1
        bands = {rep: (100 * l / t if t else 0) for rep, (l, t) in per_rep.items()}
        if bands:
            vals = list(bands.values())
            print(f"\nA0 per-rep lost%: { {k:round(v) for k,v in bands.items()} } "
                  f"band=[{min(vals):.0f},{max(vals):.0f}] (sanity check; paired test decides)")

    if control:
        print(f"\ncontrols {control}:")
        for arm in arms:
            for repo in control:
                m = agg(repo, arm)
                if not m:
                    continue
                nch = len(m); lost = sum(not v["usable"] for v in m.values())
                concl = sum(v["concl"] for v in m.values())
                print(f"  {repo:16} {arm}: chunks={nch} concl={100*concl/nch:.0f}% "
                      f"lost={100*lost/nch:.0f}%")

    print("\nGATES: SHIP an arm if concl% up + McNemar p<~0.05 one-sided (c>b) + "
          "find_ratio>=0.90 + controls not regressed. REFUTE if p ns, or find_ratio<0.90 "
          "(suppression), regardless of concl gain.")


if __name__ == "__main__":
    main()
