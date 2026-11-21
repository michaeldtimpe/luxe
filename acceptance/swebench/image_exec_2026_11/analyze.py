#!/usr/bin/env python3
"""G3 analysis for the image-exec slice (control / fo / ix x 25 x 3).

Reads slice/<arm>-rep<N>/<iid>.json (predictions + run ids) and
slice/<arm>-rep<N>/harness/harness_summary.json (grades), plus the subset's
9-rep prior record and roles (benchmarks/swebench/subsets/fo_slice_2026_11.json)
and each run's events.jsonl / messages.jsonl.gz under ~/.luxe/runs/.

Prints: per-arm resolved (all / flip targets / no-harm), per-instance grid,
flips (resolved in an arm where the prior record is 0/9, or above the prior
rate), no-harm net losses vs control, liveness, wall, and the list of NEW
passes to read by hand (resolved in fo/ix but never in control here and
<= 1/9 prior). Writes slice/analysis.json.

  python3 acceptance/swebench/image_exec_2026_11/analyze.py
"""
from __future__ import annotations

import gzip
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
S = HERE / "slice"
SUB = HERE.parents[2] / "benchmarks/swebench/subsets/fo_slice_2026_11.json"
RUNS = Path.home() / ".luxe/runs"
ARMS = ("control", "fo", "ix")
REPS = (1, 2, 3)
TESTISH = re.compile(r"pytest|runtests\.py|unittest|bin/test")


def prior_rate(s: str) -> float:
    if s == "never":
        return 0.0
    m = re.search(r"(\d+)/(\d+)", s)
    return int(m.group(1)) / int(m.group(2)) if m else float("nan")


def events(rid: str) -> list[dict]:
    p = RUNS / rid / "events.jsonl"
    return [json.loads(x) for x in p.open()] if p.exists() else []


def bash_cmds(rid: str) -> list[tuple[str, str]]:
    p = RUNS / rid / "messages.jsonl.gz"
    if not p.exists():
        return []
    msgs = [json.loads(x) for x in gzip.open(p, "rt")]
    res = {m.get("tool_call_id"): m.get("content") or "" for m in msgs if m.get("role") == "tool"}
    out = []
    for m in msgs:
        for tc in (m.get("tool_calls") or []) if m.get("role") == "assistant" else []:
            if tc["function"]["name"] == "bash":
                try:
                    c = json.loads(tc["function"]["arguments"] or "{}").get("command", "")
                except json.JSONDecodeError:
                    c = ""
                out.append((c, res.get(tc.get("id"), "")))
    return out


def main() -> None:
    sub = json.loads(SUB.read_text())
    ids, prior, role = sub["instance_ids"], sub["per_instance"], sub["per_instance_role"]
    grid: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    live = defaultdict(lambda: defaultdict(int))
    walls = defaultdict(list)
    django = defaultdict(lambda: defaultdict(int))
    missing = []
    for arm in ARMS:
        for rep in REPS:
            cell = S / f"{arm}-rep{rep}"
            hs = cell / "harness" / "harness_summary.json"
            graded = json.loads(hs.read_text())["instances"] if hs.exists() else {}
            if not graded:
                missing.append(cell.name)
            for iid in ids:
                pj = cell / f"{iid}.json"
                d = json.loads(pj.read_text()) if pj.exists() else {}
                g = graded.get(iid, {})
                grid[iid][arm].append(bool(g.get("resolved")) if g else None)
                walls[arm].append(d.get("wall_s", 0))
                rid = d.get("luxe_run_id", "")
                ev = events(rid)
                st = [e for e in ev if e["kind"] == "image_exec_stats"]
                live[arm]["runs"] += 1
                live[arm]["image_runs"] += bool(st and st[0].get("image_calls"))
                live[arm]["image_errors"] += sum(e["kind"] == "image_exec_error" for e in ev)
                live[arm]["fo_shown"] += sum(e["kind"] == "failed_output_shown" for e in ev)
                live[arm]["empty_patch"] += not (d.get("model_patch") or "").strip()
                live[arm]["scratch_stripped"] += len(d.get("scratch_stripped") or [])
                if iid.startswith("django__"):
                    for c, r in bash_cmds(rid):
                        if "pytest" in c:
                            django[arm]["pytest"] += 1
                            django[arm]["pytest_missing"] += "No module named pytest" in r
                        if "runtests.py" in c:
                            django[arm]["runtests"] += 1

    def resolved(arm, sel=lambda i: True):
        return sum(v for i in ids if sel(i) for v in grid[i][arm] if v)

    print("cells without grades:", missing or "none")
    print("\narm      resolved/75  flip_targets/39  no_harm/36  median_wall  image_runs  image_err  fo_shown  empty  scratch")
    for arm in ARMS:
        print(f"{arm:8s} {resolved(arm):6d}       {resolved(arm, lambda i: role[i] == 'flip_target'):6d}"
              f"          {resolved(arm, lambda i: role[i] == 'no_harm'):6d}"
              f"      {statistics.median(walls[arm]):7.0f}s  {live[arm]['image_runs']:4d}/{live[arm]['runs']}"
              f"    {live[arm]['image_errors']:4d}    {live[arm]['fo_shown']:5d}   {live[arm]['empty_patch']:3d}   {live[arm]['scratch_stripped']:3d}")

    print("\nper instance (prior | control fo ix as resolved/3):")
    flips, losses, new_pass = [], [], []
    for i in sorted(ids, key=lambda x: (role[x], x)):
        c = {a: sum(1 for v in grid[i][a] if v) for a in ARMS}
        print(f"  {role[i]:11s} {i:38s} {prior[i]:>11s} | {c['control']} {c['fo']} {c['ix']}")
        pr = prior_rate(prior[i])
        for a in ("fo", "ix"):
            if role[i] == "flip_target" and c[a] / 3 > pr:
                flips.append((a, i, prior[i], c[a], c["control"]))
            if role[i] == "no_harm" and c[a] < c["control"]:
                losses.append((a, i, c["control"], c[a]))
            if c[a] and not c["control"] and pr <= 1 / 9:
                new_pass.append((a, i, [r for r, v in zip(REPS, grid[i][a]) if v]))
    print("\nflips (arm, iid, prior, arm/3, control/3):")
    for f in flips:
        print("  ", *f)
    print("\nno-harm losses vs control (arm, iid, control/3, arm/3):", losses or "none")
    for a in ("fo", "ix"):
        net = sum(c - x for aa, _, c, x in losses if aa == a)
        print(f"  {a}: net no-harm loss {net} (gate: ix <= 1)")
    print("\nNEW passes to hand-read (arm, iid, reps):")
    for n in new_pass:
        print("  ", *n)
    print("\nDjango test commands (pytest absent in testbed):", {a: dict(v) for a, v in django.items()})
    (S / "analysis.json").write_text(json.dumps({
        "grid": {i: dict(v) for i, v in grid.items()}, "liveness": {a: dict(v) for a, v in live.items()},
        "flips": flips, "noharm_losses": losses, "new_pass": new_pass,
        "django": {a: dict(v) for a, v in django.items()}}, indent=1, default=str))


if __name__ == "__main__":
    main()
