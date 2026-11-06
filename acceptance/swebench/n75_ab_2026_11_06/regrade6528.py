# Regrade pylint-6528 alone for control (== unbudgeted patch, byte-identical) and ctxonly.
import json, os
from pathlib import Path
from benchmarks.swebench.harness import run_harness
U = Path(os.environ["HOME"]) / "Downloads/luxe/acceptance/swebench/n75_ab_2026_11_06"
for arm in ("control", "ctxonly"):
    sel = [r for r in json.load(open(U / arm / "predictions.json")) if r["instance_id"] == "pylint-dev__pylint-6528"]
    out = U / arm / "harness_regrade_6528"; out.mkdir(exist_ok=True)
    (out / "predictions.json").write_text(json.dumps(sel)); os.chdir(out)
    r = run_harness(out / "predictions.json", output_dir=out, run_id=f"r6528_{arm}", max_workers=1, timeout_per_instance_s=3600)
    print("RESULT", arm, {k: (v.resolved, v.error) for k, v in r.items()}, flush=True)
print("REGRADE6528 DONE", flush=True)
