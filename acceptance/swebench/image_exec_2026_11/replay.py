"""G1 offline replay for SWE-bench image exec (no model).

Re-feeds every bash command the champion issued in the diag_misses_2026_11
control rep (46 instances) through luxe's REAL bash path twice:

  old: shipped defaults (no image exec, no noop strip) - validator verdict only
  new: image exec on + LUXE_BASH_NOOP=strip (the ix arm) - verdict, and with
       --exec the routed python/pytest/pip commands actually run in the
       instance image on that run's final tree (base_commit + model_patch).

Nothing here re-implements the routing: `shell._bash` is called with
`_run_in_image` stubbed (classify) or live (--exec).

Run from the luxe-ix (or main, once landed) checkout:
  PYTHONPATH=$PWD/src:$PWD .venv/bin/python -I acceptance/swebench/image_exec_2026_11/replay.py [--exec]
Writes replay.json + prints the summary table.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from luxe.tools import fs, shell  # noqa: E402
from benchmarks.swebench.adapter import instance_image  # noqa: E402

DIAG = Path.home() / "Downloads/luxe/acceptance/swebench/diag_misses_2026_11"
RAW = Path.home() / "Downloads/luxe/benchmarks/swebench/subsets/raw/verified.jsonl"
WS = Path.home() / ".luxe/swebench-workspace"
TESTISH = re.compile(r"\b(pytest|runtests\.py|unittest|tox|bin/test)\b|python\S* -m pytest")


def bash_cmds(run_id: str) -> list[str]:
    p = Path.home() / ".luxe/runs" / run_id / "messages.jsonl.gz"
    if not p.exists():
        return []
    out = []
    for line in gzip.open(p, "rt"):
        m = json.loads(line)
        for tc in (m.get("tool_calls") or []) if m.get("role") == "assistant" else []:
            fn = tc.get("function", {})
            if fn.get("name") == "bash":
                try:
                    out.append(json.loads(fn.get("arguments") or "{}").get("command", ""))
                except json.JSONDecodeError:
                    out.append(fn.get("arguments") or "")
    return out


def verdict(cmd: str) -> tuple[str, str]:
    """('refused'|'host'|'image', detail) through the current shell state, with
    execution stubbed: host commands are not run, image commands not sent."""
    routed = {}

    def fake_image(command, repo_root, cwd_rel):
        routed["cmd"] = command
        return "", None
    real_image, real_popen = shell._run_in_image, subprocess.Popen

    class NoRun:  # host execution stub: record and return like a clean exit
        def __init__(self, command, *a, **k):
            routed["host"] = command
            self.returncode = 0

        def communicate(self, timeout=None):
            return "", ""
    shell._run_in_image = fake_image
    subprocess.Popen = NoRun
    try:
        out, err = shell._bash({"command": cmd})
    finally:
        shell._run_in_image, subprocess.Popen = real_image, real_popen
    if "cmd" in routed:
        return "image", routed["cmd"]
    if "host" in routed:
        return "host", routed["host"]
    return "refused", (err or "")[:120]


def final_tree(iid: str, base: str, patch: str, tmp: Path) -> Path:
    src = WS / iid / "repo"
    d = tmp / iid
    subprocess.run(["git", "clone", "-q", "--local", "--no-checkout", str(src), str(d)], check=True)
    subprocess.run(["git", "checkout", "-q", "-f", base], cwd=d, check=True)
    if patch.strip():
        subprocess.run(["git", "apply", "-"], input=patch, text=True, cwd=d, check=True)
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exec", action="store_true", help="run routed commands in the image")
    a = ap.parse_args()
    base_of = {}
    for line in RAW.open():
        r = json.loads(line)
        base_of[r["instance_id"]] = r["base_commit"]
    rows = []
    tmp = Path(tempfile.mkdtemp(prefix="ix-replay-"))
    for f in sorted((DIAG / "control").glob("*__*.json")):
        c = json.loads(f.read_text())
        iid, base = c["instance_id"], base_of[c["instance_id"]]
        cmds = bash_cmds(c.get("luxe_run_id", ""))
        if not cmds:
            continue
        tree = final_tree(iid, base, c.get("model_patch", ""), tmp)
        fs.set_repo_root(tree)
        # old = shipped defaults
        shell.set_image_exec(None, None)
        shell.set_noop_mode(None)
        old = [verdict(x) for x in cmds]
        # new = ix arm
        shell.set_noop_mode("strip")
        shell.set_image_exec(instance_image(iid), base)
        new = [verdict(x) for x in cmds]
        for i, x in enumerate(cmds):
            row = {"iid": iid, "i": i, "cmd": x, "old": old[i][0], "new": new[i][0],
                   "new_cmd": new[i][1], "testish": bool(TESTISH.search(x))}
            if a.exec and new[i][0] == "image":
                t0 = time.monotonic()
                out, err = shell._bash({"command": x})
                row.update(exec_rc=err or "0", exec_s=round(time.monotonic() - t0, 1),
                           exec_head=out[:400], exec_tail=out[-400:],
                           import_error=bool(re.search(
                               r"ModuleNotFoundError|ImportError|No module named", out)))
            rows.append(row)
        shell.set_image_exec(None, None)
        shell.set_noop_mode(None)
    fs._REPO_ROOT = None
    (HERE / ("replay_exec.json" if a.exec else "replay.json")).write_text(json.dumps(rows, indent=1))

    n = len(rows)
    old_ref = [r for r in rows if r["old"] == "refused"]
    print(f"bash calls: {n} across {len({r['iid'] for r in rows})} instances")
    print(f"old refused: {len(old_ref)}   new refused: {sum(r['new'] == 'refused' for r in rows)}")
    rescued = [r for r in old_ref if r["new"] != "refused"]
    print(f"refused -> runnable: {len(rescued)}/{len(old_ref)} "
          f"({100 * len(rescued) / max(1, len(old_ref)):.0f}%)  [G1 abort below 40%]")
    print("new routing:", dict(Counter(r["new"] for r in rows)))
    print("testish calls: old ran", sum(r["testish"] and r["old"] != "refused" for r in rows),
          "| new ran", sum(r["testish"] and r["new"] != "refused" for r in rows))
    still = Counter(re.sub(r"[`'\"].*", "", r["new_cmd"])[:60] if r["new"] == "refused" else None
                    for r in rows)
    still.pop(None, None)
    print("still refused, top reasons:")
    for k, v in still.most_common(6):
        print(f"  {v:3d}  {k}")
    if a.exec:
        ex = [r for r in rows if "exec_rc" in r]
        print(f"executed in image: {len(ex)}; rc0 {sum(r['exec_rc'] == '0' for r in ex)}; "
              f"import errors {sum(r['import_error'] for r in ex)}; "
              f"unavailable {sum(str(r['exec_rc']).startswith('image exec unavailable') for r in ex)}; "
              f"median s {sorted(r['exec_s'] for r in ex)[len(ex) // 2] if ex else 0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
