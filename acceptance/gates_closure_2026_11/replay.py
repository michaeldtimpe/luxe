import sys,json,glob,subprocess,tempfile,os,re
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pathlib import Path
from benchmarks.maintain_suite.grade import _parse_diff_hunks,_unfenced_headings
from benchmarks.maintain_suite.gates import check_destructive_deletion as cdd, check_tool_output_echo as cte
WS=Path.home()/'.luxe/bench-workspace'
def show(repo,sha,path):
    r=subprocess.run(['git','show',f'{sha}:{path}'],cwd=repo,capture_output=True,text=True);return r.stdout if r.returncode==0 else None
def heads(t): return _unfenced_headings(t) if t is not None else None
def gate(hunks,adds,dels,dh):
    return cdd(adds,dels,diff_hunks=hunks,task_type='document',doc_headings=dh),cte(hunks)
def run_patch(patch,repo,base):
    hunks=_parse_diff_hunks(Path(patch).read_text())
    adds=sum(len(a) for a,_ in hunks.values());dels=sum(len(d) for _,d in hunks.values())
    dh={}
    for p in hunks:
        if p.lower().endswith(('.md','.markdown')):
            b=show(repo,base,p)
            with tempfile.TemporaryDirectory() as t:
                if b is not None:
                    Path(t,p).parent.mkdir(parents=True,exist_ok=True);Path(t,p).write_text(b)
                    r=subprocess.run(['git','apply','--unsafe-paths','--directory=.','-'],input=Path(patch).read_text(),cwd=t,capture_output=True,text=True)
                    h=heads(Path(t,p).read_text()) if r.returncode==0 else None
                else: h=None
            dh[p]=(heads(b),h)
    return adds,dels,gate(hunks,adds,dels,dh)
print('== POSITIVE reconstructions on real base files')
def synth(fix,sha,path,newtext,label):
    repo=WS/f'{fix}-clone';b=show(repo,sha,path)
    # build hunks as whole-file replace
    old=b.splitlines();new=newtext.splitlines()
    hunks={path:(new,old)}
    dh={path:(heads(b),heads(newtext))}
    (d,dd),(e,ed)=gate(hunks,len(new),len(old),dh)
    print(f'{label}: +{len(new)}/-{len(old)} destructive_diff={d} [{dd[:110]}] echo={e} [{ed[:80]}]')
nb='ef953cc85bead2f09147b99c2a1a2cb2f7d813d0';p='ARCHITECTURE.md'
b=show(WS/'neon-rain-document-modules-clone',nb,p)
echo=''.join(f'{i+1}\t{l}\n' for i,l in enumerate(b.splitlines()))+'## Subdirectories\nx\n'
synth('neon-rain-document-modules',nb,p,echo,'neon-rain echo rewrite (documented 08-17 failure)')
# pure replace-in-place overwrite, unrelated vocabulary, same size, headings kept
import random
random.seed(1);vocab=[f'zq{n}x' for n in range(400)]
ov='\n'.join((l if l.startswith('#') or not l.strip() else ' '.join(random.choice(vocab) for _ in l.split())) for l in b.splitlines())+'\n'
synth('neon-rain-document-modules',nb,p,ov,'neon-rain prong2 overwrite (headings kept, words replaced)')
gb='514d22d95dbbc55b8ad73d158d311f507f8a7b51'
g=show(WS/'the-game-document-architecture-clone',gb,'README.md')
lines=g.splitlines();i=[k for k,l in enumerate(lines) if l.strip()=='## Features'][0]
j=next((k for k in range(i+1,len(lines)) if lines[k].startswith('## ')),len(lines))
new=lines[:i]+['## How It Works','','The pick is chosen in src/App.jsx.','']+lines[j:]
synth('the-game-document-architecture',gb,'README.md','\n'.join(new)+'\n','the-game Features->How It Works swap (08-17 failure)')
print('== NEGATIVE: real champion/ref document diffs')
rows=[]
for pth in sorted(glob.glob('acceptance/harness_ab_2026_10/*/champ/rep*/*document*/diff.patch')+glob.glob('acceptance/thinkingcap_27b_2026_10/r1/*qwen3.6-27b-6bit-ref*/*document*/diff.patch')):
    fx=pth.split('/')[-2];mp=Path(pth).parent/'meta.json'
    if mp.exists(): base=json.load(open(mp))['base_sha']
    else:
        base=json.load(open(Path(pth).parent/'state.json'))['base_sha_used']
    repo=WS/f'{fx}-clone'
    if not repo.exists(): print('noclone',fx);continue
    a,d,((x,xd),(e,ed))=run_patch(pth,repo,base)
    rows.append((pth,a,d,x,e))
    print('/'.join(pth.split('/')[1:-1]),f'+{a}/-{d}','destr',x,'echo',e,(xd[:70] if x else ''))
print('N',len(rows),'fired',sum(1 for r in rows if r[3] or r[4]))
