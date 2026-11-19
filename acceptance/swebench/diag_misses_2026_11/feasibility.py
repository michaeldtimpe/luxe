#!/usr/bin/env python3
"""Feasibility probe: can the gold F2P tests be collected on host venv vs SWE-bench image.
Read-only on the workspace (local clone into scratch). No model runs.
Usage: feasibility.py <scratch_feas_dir> <out_json>"""
import json, os, re, shutil, statistics, subprocess, sys, time
from pathlib import Path

ROOT = Path.home() / "Downloads/luxe"
SCR = Path(sys.argv[1]); OUT = Path(sys.argv[2])
WS = Path.home() / ".luxe/swebench-workspace"
PY = str(ROOT / ".venv/bin/python")
ENV = dict(os.environ, DOCKER_HOST=f"unix://{Path.home()}/.colima/default/docker.sock")
ids = json.load(open(ROOT / "benchmarks/swebench/subsets/diag_misses_2026_11.json"))["instance_ids"]
gold = {}
for l in open(ROOT / "benchmarks/swebench/subsets/raw/verified.jsonl"):
    r = json.loads(l)
    if r["instance_id"] in ids: gold[r["instance_id"]] = r
images = set(subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True, text=True, env=ENV).stdout.split())

def files_of(tp):
    return sorted(set(re.findall(r"^diff --git a/(\S+) b/", tp, re.M)))

def classify(rc, out, django):
    if django:
        ok = re.search(r"^Ran \d+ test", out, re.M) is not None
    else:
        ok = rc == 0
    if ok: return "ok", ""
    pats = [r"^E\s+(\w*(Error|Exception)\b.*)", r"^((ModuleNotFound|Import)Error.*)", r"^(\w*Error: .*)"]
    for p in pats:
        m = re.search(p, out, re.M)
        if m: return "error", m.group(1).strip()[:140]
    return "error", (out.strip().splitlines() or ["no output"])[-1][:140]

def cmd_for(r, files, f2p):
    django = r["repo"] == "django/django"
    if django:
        labels = sorted({".".join(re.sub(r"^(\w+) \((.+)\)$", r"\2", t).split(".")[:-1]) if " (" not in t else re.sub(r"^\w+ \((.+)\)$", r"\1", t).rsplit(".", 1)[0] for t in f2p})
        labels = sorted({re.sub(r"^\w+ \((.+)\)$", r"\1", t) for t in f2p})
        return ["python", "tests/runtests.py", "--parallel", "1", *labels], True
    return ["python", "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *[f for f in files if f.endswith(".py")]], False

def sh(args, cwd=None, timeout=120):
    t = time.time()
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=ENV)
        return p.returncode, p.stdout + p.stderr, time.time() - t
    except subprocess.TimeoutExpired:
        return 124, "timeout", time.time() - t

res = []; lat = []
SCR.mkdir(parents=True, exist_ok=True)
for iid in ids:
    r = gold[iid]; f2p = json.loads(r["FAIL_TO_PASS"]); files = files_of(r["test_patch"])
    cmd, django = cmd_for(r, files, f2p)
    row = {"iid": iid, "n_f2p": len(f2p)}
    # (i) host
    d = SCR / iid
    try:
        if d.exists(): shutil.rmtree(d)
        src = WS / iid / "repo"
        rc, o, _ = sh(["git", "clone", "-q", "--local", "--no-checkout", str(src), str(d)])
        if rc: raise RuntimeError("clone: " + o[:100])
        rc, o, _ = sh(["git", "checkout", "-q", "-f", r["base_commit"]], cwd=d)
        if rc: raise RuntimeError("checkout: " + o[:100])
        (d / "_t.patch").write_text(r["test_patch"])
        rc, o, _ = sh(["git", "apply", "_t.patch"], cwd=d)
        if rc: raise RuntimeError("apply test_patch: " + o[:100])
        hc = [PY if c == "python" else c for c in cmd]
        rc, o, _ = sh(hc, cwd=d)
        row["host"], row["host_err"] = classify(rc, o, django)
    except Exception as e:
        row["host"], row["host_err"] = "error", str(e)[:140]
    # (ii) image
    img = "swebench/sweb.eval.x86_64." + iid.replace("__", "_1776_") + ":latest"
    if img in images:
        inner = ("cd /testbed && git checkout -q -f %s && printf %%s %s | base64 -d > /tmp/t.patch && git apply /tmp/t.patch && "
                 "source /opt/miniconda3/bin/activate testbed && %s") % (
            r["base_commit"], __import__("shlex").quote(__import__("base64").b64encode(r["test_patch"].encode()).decode()),
            " ".join(__import__("shlex").quote(c) for c in cmd))
        rc, o, dt = sh(["docker", "run", "--rm", "--platform", "linux/amd64", img, "bash", "-lc", inner], timeout=300)
        row["image"], row["image_err"] = classify(rc, o, django); row["image_s"] = round(dt, 1)
        rc, o, dt = sh(["docker", "run", "--rm", "--platform", "linux/amd64", img, "true"])
        lat.append(dt)
    else:
        row["image"], row["image_err"] = "no image", ""
    res.append(row); print(iid, row["host"], row["image"], flush=True)
    shutil.rmtree(d, ignore_errors=True)
json.dump({"rows": res, "trivial_run_s": lat}, open(OUT, "w"), indent=1)
