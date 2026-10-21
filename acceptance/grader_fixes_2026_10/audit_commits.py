"""Prong 3 old vs new over the last N commits of a repo (task_type=document)."""
import subprocess, sys
from benchmarks.maintain_suite import grade as g

repo, n = sys.argv[1], int(sys.argv[2])
def git(*a, ok=False):
    p = subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, errors="replace")
    if p.returncode and not ok: raise SystemExit(p.stderr)
    return p.returncode, p.stdout
_, revs = git("rev-list", "--no-merges", f"-n{n}", "HEAD")
old_f, new_f, cands = [], [], 0
for c in revs.split():
    _, out = git("show", "-M", "--format=", c)
    hunks = g._parse_diff_hunks(out)
    if not any(p.lower().endswith(tuple(g._MARKDOWN_EXTS)) for p in hunks): continue
    cands += 1
    dh = {}
    for p in hunks:
        if not p.lower().endswith(tuple(g._MARKDOWN_EXTS)): continue
        sides = []
        for rev in (f"{c}^", c):
            rc, t = git("show", f"{rev}:{p}", ok=True)
            sides.append(g._unfenced_headings(t) if rc == 0 else None)
        dh[p] = tuple(sides)
    o = g._check_doc_section_loss(hunks, "document")
    nw = g._check_doc_section_loss(hunks, "document", dh)
    if o[0]: old_f.append((c[:8], o[1][:150]))
    if nw[0]: new_f.append((c[:8], nw[1][:150]))
print(f"commits={len(revs.split())} touching markdown={cands} old_fires={len(old_f)} new_fires={len(new_f)}")
newset = {c for c, _ in new_f}
for c, d in old_f:
    print(("KEPT   " if c in newset else "DROPPED"), c, d)
for c, d in new_f:
    if c not in {x for x, _ in old_f}: print("ADDED  ", c, d)
