"""Cache A/B analysis: per arm score + wall + tokens (bench files) and prefix-
cache behaviour (step_usage / compaction events in ~/.luxe/runs/<id>)."""
import json, glob, os, sys, statistics, collections
D = os.path.dirname(os.path.abspath(__file__))
ARMS = [a for a in ["base", "R", "C", "RC"] if os.path.isdir(f"{D}/{a}")]
RUNS = os.path.expanduser("~/.luxe/runs")

def load(arm):
    rows = []
    for st in glob.glob(f"{D}/{arm}/mono__rep*/*/state.json"):
        fdir = os.path.dirname(st); rep = fdir.split("/")[-2].split("__")[1]
        s = json.load(open(st))
        try: r = json.load(open(f"{fdir}/result.json"))
        except Exception: r = {}
        try: dg = json.load(open(f"{fdir}/diagnostics.json"))
        except Exception: dg = {}
        ev = []
        p = f"{RUNS}/{s.get('luxe_run_id','')}/events.jsonl"
        if s.get("luxe_run_id") and os.path.exists(p):
            ev = [json.loads(l) for l in open(p) if l.strip()]
        rows.append(dict(fixture=s["fixture_id"], rep=rep, score=r.get("score", 0),
                         passed=bool(r.get("expected_outcome_passed")) and not r.get("error"),
                         wall=dg.get("wall_s", 0.0), tokens=dg.get("tokens_total", 0),
                         prompt=dg.get("prompt_tokens", 0), events=ev))
    return rows

def cache_stats(rows):
    c = collections.Counter(); prefill = decode = 0.0
    for r in rows:
        su = [e for e in r["events"] if e["kind"] == "step_usage" and e.get("phase") == "main"]
        prev = None
        comp_steps = {str(e.get("step")) for e in r["events"] if e["kind"] == "compaction_phase_reached"}
        for e in su:
            p, k = e["prompt_tokens"] or 0, e["cached_tokens"]
            c["prompt"] += p; c["cached"] += k or 0; c["steps"] += 1
            prefill += e.get("prefill_s") or 0; decode += e.get("decode_s") or 0
            c["reasoning_chars"] += e.get("reasoning_chars") or 0
            if prev is not None and k is not None:
                # expected reuse = previous prompt minus the generation-prompt tail
                exp = prev - 8
                if k >= exp: c["warm"] += 1
                elif k == 0: c["cold"] += 1
                else:
                    c["partial"] += 1; c["lost_tokens"] += exp - k
                    if str(e.get("step")) in comp_steps:
                        c["partial_after_compaction"] += 1; c["lost_compaction"] += exp - k
            prev = p
        c["compaction_fires"] += sum(1 for e in r["events"] if e["kind"] == "compaction_phase_reached")
    c["prefill_s"] = round(prefill, 1); c["decode_s"] = round(decode, 1)
    return c

data = {a: load(a) for a in ARMS}
out = []
P = out.append
P("| arm | runs | pass | score | avg wall s | avg tokens | cached % | prefill s (sum) | decode s (sum) | cont. steps warm/partial (of which compaction)/cold | re-prefilled tokens (compaction) | compaction fires | reasoning chars |")
P("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for a, rows in data.items():
    if not rows: continue
    cs = cache_stats(rows)
    P(f"| {a} | {len(rows)} | {sum(r['passed'] for r in rows)}/{len(rows)} | {sum(r['score'] for r in rows)}/{5*len(rows)} | "
      f"{statistics.mean(r['wall'] for r in rows):.1f} | {statistics.mean(r['tokens'] for r in rows):,.0f} | "
      f"{cs['cached']/max(cs['prompt'],1):.1%} | {cs['prefill_s']} | {cs['decode_s']} | "
      f"{cs['warm']}/{cs['partial']} ({cs['partial_after_compaction']})/{cs['cold']} | {cs['lost_tokens']:,} ({cs['lost_compaction']:,}) | {cs['compaction_fires']} | {cs['reasoning_chars']:,} |")
P("")
fixtures = sorted({r["fixture"] for rows in data.values() for r in rows})
P("Per fixture (passes/reps [scores]  avg wall):")
P("```")
P(f"{'fixture':40}" + "".join(f"{a:>26}" for a in data))
for f in fixtures:
    line = f"{f:40}"
    for a, rows in data.items():
        rs = sorted([r for r in rows if r["fixture"] == f], key=lambda r: r["rep"])
        if not rs: line += f"{'-':>26}"; continue
        cell = f"{sum(r['passed'] for r in rs)}/{len(rs)} {[r['score'] for r in rs]} {statistics.mean(r['wall'] for r in rs):.0f}s"
        line += f"{cell:>26}"
    P(line)
P("```")
print("\n".join(out))
if "--json" in sys.argv:
    json.dump({a: [{k: v for k, v in r.items() if k != "events"} for r in rows] for a, rows in data.items()},
              open(f"{D}/runs_index.json", "w"), indent=1)
