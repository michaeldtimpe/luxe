# Run luxe's code+chat drills with the scratch repo PRESERVED, so the diff and
# pytest output can be read by hand (the drill deletes it on success).
import shutil, os, time
import luxe.chat.smoke as smoke
from luxe.config import load_config

kept = []
_real = smoke._make_drill_repo
def _keep(kind, files):
    r = _real(kind, files)
    kept.append(r)
    return r
smoke._make_drill_repo = _keep
shutil.rmtree = lambda *a, **k: None

cfg = load_config(os.environ["LUXE_CONFIG"])
for name, fn in (("chat", smoke.run_chat_drill), ("code", smoke.run_code_drill)):
    t0 = time.time()
    rep = fn(cfg)
    print("### %s drill  wall=%.1fs" % (name, time.time() - t0))
    for c in rep.steps:
        print("  [%s] %s: %s" % (c.state, c.name, c.detail))
print("KEPT:", [str(p) for p in kept])
