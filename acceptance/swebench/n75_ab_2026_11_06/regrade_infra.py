# Regrade infra-suspect instances (psf__requests: live httpbin; pylint-6528: no_report).
import json, os, sys
from pathlib import Path
from benchmarks.swebench.harness import run_harness
U = Path(os.environ["HOME"]) / "Downloads/luxe/acceptance/swebench/n75_ab_2026_11_06"
for arm in ("control", "ctxonly", "unbudgeted"):
    rows = json.load(open(U / arm / "predictions.json"))
    sel = [r for r in rows if (r["instance_id"].startswith("psf__requests") or r["instance_id"] == "pylint-dev__pylint-6528") and (r.get("model_patch") or "").strip()]
    out = U / arm / "harness_regrade_infra"
    out.mkdir(exist_ok=True)
    (out / "predictions.json").write_text(json.dumps(sel))
    os.chdir(out)
    r = run_harness(out / "predictions.json", output_dir=out, run_id=f"regrade_{arm}")
    res = {k: {"resolved": v.resolved, "error": v.error} for k, v in sorted(r.items())}
    (out / "regrade_summary.json").write_text(json.dumps(res, indent=1))
    print(arm, json.dumps(res), flush=True)
