"""Per run: branch diff vs base_sha in the fixture repo — files, +/-, deleted files,
commit time vs run window. Flags anything that could be a destructive 'pass'."""
import json, glob, os, subprocess, sys, yaml
D = os.path.expanduser("~/luxe-bench-tmp/cache-ab/acceptance/cache_ab_2026_10")
FX = {f["id"]: f for f in yaml.safe_load(open(os.path.expanduser(
    "~/luxe-bench-tmp/cache-ab/benchmarks/maintain_suite/fixtures.yaml")))["fixtures"]}
RUNS = os.path.expanduser("~/.luxe/runs")
def g(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout
out = {}
for st in sorted(glob.glob(f"{D}/*/mono__rep*/*/state.json")):
    rel = os.path.relpath(st, D).split("/"); arm, rep, fx = rel[0], rel[1][-4:], rel[2]
    if arm == "probe_RC": continue
    if not os.path.exists(os.path.dirname(st) + "/result.json"): continue
    s = json.load(open(st)); r = json.load(open(os.path.dirname(st) + "/result.json"))
    rid = s["luxe_run_id"]; repo = FX[fx]["repo_url"]
    br = json.load(open(f"{RUNS}/{rid}/pr_state.json"))["branch_name"]
    base = s["base_sha_used"]
    ns = g(repo, "diff", "--numstat", f"{base}..{br}")
    st_ = g(repo, "diff", "--name-status", f"{base}..{br}")
    ct = g(repo, "log", "-1", "--format=%ct", br).strip()
    files = [l.split("\t") for l in ns.splitlines()]
    add = sum(int(a) for a, d, f in files if a != "-"); dele = sum(int(d) for a, d, f in files if d != "-")
    deleted = [l.split("\t")[1] for l in st_.splitlines() if l.startswith("D")]
    fresh = ct and abs(int(ct) - s["last_attempt_ts"]) < 3600
    key = f"{arm} {rep} {fx}"
    out[key] = dict(arm=arm, rep=rep, fx=fx, score=r["score"], passed=r["expected_outcome_passed"],
                    branch=br, base=base, repo=repo, files=[f for _, _, f in files], add=add,
                    dele=dele, deleted=deleted, fresh=bool(fresh))
    flag = []
    if deleted: flag.append("DELETED-FILES")
    if dele > add: flag.append("NET-NEG")
    if not fresh and files: flag.append("STALE-BRANCH?")
    print(f"{key:58} s={r['score']} +{add:<4} -{dele:<4} {','.join(f for _,_,f in files)[:70]:70} {' '.join(flag)}")
json.dump(out, open(f"{D}/verify_index.json", "w"), indent=1)
