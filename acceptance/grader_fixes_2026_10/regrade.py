"""Regrade every collected fixture diff with the old and new grade.py; print
every printed-verdict change. Only regex_present or document/manage fixtures
are graded (the two changes can't touch anything else, and tests_pass
fixtures would run their suites)."""
import hashlib, importlib, json, subprocess, sys, tempfile
from pathlib import Path
import yaml

HERE = Path(__file__).parent
ACC = Path.home() / "Downloads/luxe/acceptance"
CACHE = Path.home() / ".luxe/fixture-cache"

def load(ver):
    sys.path.insert(0, str(HERE / ver))
    for m in [k for k in sys.modules if k.startswith("benchmarks")]: del sys.modules[m]
    g = importlib.import_module("benchmarks.maintain_suite.grade")
    sys.path.pop(0)
    return g
OLD, NEW = load("old"), load("new")

raw = yaml.safe_load((HERE / "fixtures.yaml").read_text())
fx_raw = {f["id"]: f for f in (raw["fixtures"] if isinstance(raw, dict) else raw)}

corpus = []  # (source, fixture_id, patch_path)
for p in (ACC / "harness_ab_2026_10").rglob("diff.patch"):
    corpus.append(("harness_ab", p.parent.name, p))
for d in ("m5max_moe_r2_2026_10", "tuning_2026_10"):
    for p in (ACC / d).rglob("*.diff"):
        corpus.append((d, p.stem, p))

work = Path(tempfile.mkdtemp(prefix="regrade_"))
clones = {}
def clone(repo_url):
    name = Path(repo_url).name
    if name not in clones:
        dst = work / name
        subprocess.run(["git", "clone", "-q", "--shared", str(CACHE / name), str(dst)], check=True)
        clones[name] = dst
    return clones[name]

def git(repo, *a, check=True):
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True,
                          errors="replace", check=check)

seen, rows, stats = {}, [], {"graded": 0, "skipped_kind": 0, "empty": 0,
                              "apply_failed": 0, "unknown_fixture": 0}
for src, fid, patch in sorted(corpus, key=lambda x: str(x[2])):
    f = fx_raw.get(fid)
    if not f:
        stats["unknown_fixture"] += 1; continue
    eo = f.get("expected_outcome", {})
    if eo.get("kind") != "regex_present" and f.get("task_type") not in ("document", "manage"):
        stats["skipped_kind"] += 1; continue
    text = patch.read_text(errors="replace")
    if not text.strip():
        stats["empty"] += 1; continue
    key = (fid, hashlib.sha1(text.encode()).hexdigest())
    if key not in seen:
        repo = clone(f["repo_url"])
        git(repo, "checkout", "-q", "-f", "--detach", f["base_sha"])
        git(repo, "clean", "-qfdx")
        ap = git(repo, "apply", "--whitespace=nowarn", "--exclude=*.pyc", "--exclude=**/__pycache__/*", str(patch), check=False)
        if ap.returncode:
            seen[key] = None
        else:
            git(repo, "add", "-A")
            git(repo, "-c", "user.name=r", "-c", "user.email=r@x", "commit", "-q",
                "--no-verify", "-m", "regrade")
            res = []
            for g in (OLD, NEW):
                fx = g.Fixture.from_dict(f)
                r = g.grade_fixture(fx, repo, pr_url="x", pr_opened=True,
                                    citations_unresolved=0, citations_total=0,
                                    base_sha=f["base_sha"])
                res.append((bool(r.expected_outcome_passed), r.score,
                            [x["name"] for x in r.gates_triggered],
                            r.expected_outcome_detail[:140]))
            seen[key] = res
    res = seen[key]
    if res is None:
        stats["apply_failed"] += 1; continue
    stats["graded"] += 1
    (op, os_, og, od), (np_, ns, ng, nd) = res
    if (op, os_, og) != (np_, ns, ng):
        rows.append((src, fid, str(patch.relative_to(ACC)), op, os_, og, od, np_, ns, ng, nd))

print(json.dumps(stats))
print(f"changed: {len(rows)}")
for r in rows:
    src, fid, path, op, os_, og, od, np_, ns, ng, nd = r
    print(f"{fid}\t{path}\told={'PASS' if op else 'FAIL'} {os_}/5 {og}\tnew={'PASS' if np_ else 'FAIL'} {ns}/5 {ng}\t| {od}")
