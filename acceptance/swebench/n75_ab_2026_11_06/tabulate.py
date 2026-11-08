"""Tabulate the n75 read-budget A/B (2026-11-06): resolves per arm-rep, per-instance
matrix, n=75 and n=71 (excluding the subset's `ungradeable` ids), and read-budget
mechanism stats mined from ~/.luxe/runs (read-only).

Usage (from the repo root):
    python3 acceptance/swebench/n75_ab_2026_11_06/tabulate.py [--runs ~/.luxe/runs] [--json out.json]

Arm-rep dirs: rep1 = <arm>/ (control, ctxonly, unbudgeted); rep2/<arm>/, rep3/<arm>/.
Each holds predictions.json, summary.json (started_at/finished_at) and
harness/harness_summary.json.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUBSET = HERE.parents[2] / "benchmarks/swebench/subsets/v1_baseline_n75.json"


def arm_reps(HERE: Path) -> list[tuple[str, str, Path]]:
    out = []
    for arm in ("control", "ctxonly", "unbudgeted"):
        if (HERE / arm / "harness/harness_summary.json").exists():
            out.append(("rep1", arm, HERE / arm))
    for rep in ("rep2", "rep3"):
        for arm in ("control", "ctxonly"):
            if (HERE / rep / arm / "harness/harness_summary.json").exists():
                out.append((rep, arm, HERE / rep / arm))
    return out


def load(d: Path) -> dict:
    hs = json.loads((d / "harness/harness_summary.json").read_text())
    preds = {r["instance_id"]: r for r in json.loads((d / "predictions.json").read_text())}
    summ = json.loads((d / "summary.json").read_text())
    inst = {}
    for iid, p in preds.items():
        h = hs["instances"].get(iid, {})
        empty = not (p.get("model_patch") or "").strip()
        inst[iid] = {"resolved": bool(h.get("resolved")), "error": h.get("error", ""), "empty": empty}
    return {"inst": inst, "summary": summ}


def mechanism(runs: Path, window: tuple[float, float]) -> dict:
    """Per-instance read stats for runs started inside the arm's time window."""
    lo, hi = window
    per = {}
    for rd in runs.iterdir():
        rj = rd / "run.json"
        if not rj.is_file():
            continue
        try:
            meta = json.loads(rj.read_text())
            t = float(meta.get("started_at", 0))
        except (ValueError, OSError):
            continue
        if not (lo - 5 <= t <= hi + 5):
            continue
        repo = meta.get("repo_path", "")
        if "swebench-workspace/" not in repo:
            continue
        iid = repo.split("swebench-workspace/")[1].split("/")[0]
        cap, reads, capped, rbytes, steps, refused = None, 0, 0, 0, 0, 0
        ev = rd / "events.jsonl"
        if not ev.is_file():
            continue
        for line in ev.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            k = e.get("kind")
            if k == "read_budget_applied":
                cap = e.get("max_bytes")
            elif k == "tool_call":
                steps = max(steps, int(e.get("step", 0)) + 1)
                if e.get("name") == "read_file":
                    reads += 1
                    b = int(e.get("bytes_out") or 0)
                    rbytes += b
                    if cap and b >= 0.9 * cap:
                        capped += 1
                    elif b == 0 and cap and e.get("path"):
                        # Oversize refusal: 0 bytes back and the file (in the
                        # instance workspace, base state) is over the cap.
                        fp = Path(repo) / e["path"]
                        try:
                            if fp.is_file() and fp.stat().st_size > cap:
                                refused += 1
                        except OSError:
                            pass
        per[iid] = {"cap": cap, "reads": reads, "capped_reads": capped, "refused_large": refused,
                    "read_bytes": rbytes, "steps": steps}
    return per


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, default=Path.home() / ".luxe/runs")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--dir", type=Path, default=HERE, help="results dir (default: this file's dir)")
    ap.add_argument("--subset", type=Path, default=SUBSET)
    ap.add_argument("--arm", action="append", default=[],
                    help="NAME:DIR[,DIR...] (one DIR per rep); replaces the default layout")
    a = ap.parse_args()
    base = a.dir.resolve()

    sub = json.loads(a.subset.read_text())
    ids = sub["instance_ids"]
    ungr = set(sub.get("ungradeable", {}).get("instance_ids", []))

    rows, data = [], {}
    specs = arm_reps(base)
    if a.arm:
        specs = []
        for spec in a.arm:
            name, dirs = spec.split(":", 1)
            for i, dd in enumerate(dirs.split(",")):
                specs.append((f"rep{i + 1}", name, Path(dd).expanduser().resolve()))
    for rep, arm, d in specs:
        L = load(d)
        s = L["summary"]
        mech = mechanism(a.runs, (s["started_at"], s["finished_at"])) if a.runs.is_dir() else {}
        L["mech"] = mech
        data[(rep, arm)] = L
        inst = L["inst"]
        res = sum(v["resolved"] for v in inst.values())
        res71 = sum(v["resolved"] for k, v in inst.items() if k not in ungr)
        empty = sum(v["empty"] for v in inst.values())
        errs = {k: v["error"] for k, v in inst.items() if v["error"]}
        caps = sorted({m["cap"] for m in mech.values() if m["cap"]})
        rows.append((rep, arm, res, res71, empty, errs, round(s["total_wall_s"] / 60),
                     len(mech), caps,
                     sum(m["reads"] for m in mech.values()),
                     sum(m["capped_reads"] for m in mech.values()),
                     sum(m["read_bytes"] for m in mech.values()),
                     sum(m["refused_large"] for m in mech.values())))

    print("rep   arm         n75  n71  empty wall_m runs cap        reads capped refused read_MB errors")
    for r in rows:
        print(f"{r[0]:5} {r[1]:11} {r[2]:4} {r[3]:4} {r[4]:5} {r[6]:6} {r[7]:4} {str(r[8]):10} "
              f"{r[9]:5} {r[10]:6} {r[12]:7} {r[11]/1e6:7.2f} {r[5]}")

    by_arm = defaultdict(list)
    for r in rows:
        by_arm[r[1]].append(r)
    print("\narm         reps  n75 mean (sd)     n71 mean (sd)     per-rep n75")
    for arm, rs in by_arm.items():
        v75 = [r[2] for r in rs]
        v71 = [r[3] for r in rs]
        sd = lambda v: statistics.stdev(v) if len(v) > 1 else 0.0  # noqa: E731
        print(f"{arm:11} {len(rs):4}  {statistics.mean(v75):5.1f} ({sd(v75):.1f})     "
              f"{statistics.mean(v71):5.1f} ({sd(v71):.1f})     {v75}")

    # Per-instance: count of resolving reps per arm; print instances that differ.
    arms = sorted(by_arm)
    print("\nper-instance resolve counts (only rows that differ between arms or flip within an arm)")
    print("instance".ljust(36) + "".join(f"{x:>14}" for x in arms))
    matrix = {}
    for iid in ids:
        cells = {}
        for arm in arms:
            seq = "".join(
                ("E" if data[(rep, arm)]["inst"][iid]["empty"] else
                 "1" if data[(rep, arm)]["inst"][iid]["resolved"] else "0")
                for rep in ("rep1", "rep2", "rep3") if (rep, arm) in data)
            cells[arm] = seq
        matrix[iid] = cells
        vals = [c.replace("E", "0") for c in cells.values()]
        uniform = len({ch for v in vals for ch in v}) == 1
        if not uniform:
            tag = " (ungradeable)" if iid in ungr else ""
            print(iid.ljust(36) + "".join(f"{cells[x]:>14}" for x in arms) + tag)
            for arm in arms:
                ms = [data[(rep, arm)]["mech"].get(iid) for rep in ("rep1", "rep2", "rep3")
                      if (rep, arm) in data]
                print(" " * 4 + f"{arm}: " + " | ".join(
                    f"steps={m['steps']} reads={m['reads']} refused={m['refused_large']} capped={m['capped_reads']}"
                    if m else "-" for m in ms))

    if a.json:
        a.json.write_text(json.dumps({
            "rows": [dict(zip(["rep", "arm", "resolved_n75", "resolved_n71", "empty", "errors",
                               "wall_min", "runs_mined", "read_caps", "reads", "capped_reads",
                               "read_bytes", "refused_large"], r)) for r in rows],
            "matrix": matrix, "ungradeable": sorted(ungr),
            "mech": {f"{rep}/{arm}": L["mech"] for (rep, arm), L in data.items()},
        }, indent=1))


if __name__ == "__main__":
    main()
