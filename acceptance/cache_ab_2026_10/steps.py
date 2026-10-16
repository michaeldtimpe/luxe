import json, glob, os, collections, sys
D = os.path.expanduser("~/luxe-bench-tmp/cache-ab/acceptance/cache_ab_2026_10")
R = os.path.expanduser("~/.luxe/runs")
for arm in ["base", "R", "C", "RC"]:
    agg = collections.defaultdict(collections.Counter)
    for st in glob.glob(f"{D}/{arm}/mono__rep*/*/state.json"):
        if not os.path.exists(os.path.dirname(st) + "/result.json"): continue
        s = json.load(open(st)); fx = s["fixture_id"]
        ev = [json.loads(l) for l in open(f"{R}/{s['luxe_run_id']}/events.jsonl") if l.strip()]
        su = [e for e in ev if e["kind"] == "step_usage"]
        a = agg[fx]; a["runs"] += 1; a["steps"] += len(su)
        a["compl"] += sum(e["completion_tokens"] or 0 for e in su)
        a["prompt"] += sum(e["prompt_tokens"] or 0 for e in su)
        a["rc"] += sum(e.get("reasoning_chars") or 0 for e in su)
        a["comp"] += sum(1 for e in ev if e["kind"] == "compaction_phase_reached")
    if not agg: continue
    tot = sum(agg.values(), collections.Counter())
    print(arm, "steps/run %.1f compl/run %.0f prompt/run %.0f compactions %d" % (
        tot["steps"]/tot["runs"], tot["compl"]/tot["runs"], tot["prompt"]/tot["runs"], tot["comp"]))
    for fx, a in sorted(agg.items()):
        print("   %-42s steps %5.1f compl %6.0f rchars %6.0f comp %.1f" % (
            fx, a["steps"]/a["runs"], a["compl"]/a["runs"], a["rc"]/a["runs"], a["comp"]/a["runs"]))
