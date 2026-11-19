# Run from the repo root: python3 acceptance/gates_closure_2026_11/scan.py
# Scans acceptance/**/result.json and runs.json for gate firings.
import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import json,glob,collections
G=('destructive_diff','tool_output_echo')
tot=collections.Counter();fires=[];per=collections.defaultdict(lambda:[0,0])
def gl(x):
    if isinstance(x,list): return [g if isinstance(g,str) else str(g) for g in x]
    return [str(x)]
for f in glob.glob('**/result*.json',recursive=True)+glob.glob('**/runs.json',recursive=True):
    d=f.split('/')[0]
    if d in('swebench',) or d<'a':continue
    try:j=json.load(open(f))
    except Exception:continue
    rows=j if isinstance(j,list) else [j]
    for r in rows:
        if not isinstance(r,dict):continue
        g=r.get('gates_triggered',r.get('gates'))
        if g is None: continue
        if 'result' in os.path.basename(f) and os.path.basename(f)!='result.json': continue  # skip regraded dup
        per[d][0]+=1
        s=json.dumps(g)
        for k in G:
            if k in s: fires.append((d,k,f,r.get('fixture_id',r.get('fixture')),r.get('score'),r.get('max_score'),r.get('passed',r.get('expected_outcome_passed')),r.get('label') or r.get('arm')))
for d,v in sorted(per.items()):print(d,v[0])
print(len(fires))
for x in fires:print(x)
