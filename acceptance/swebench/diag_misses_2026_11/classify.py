#!/usr/bin/env python3
"""Per-instance miss classification for the diag_misses_2026_11 control rep.

Read-only. Inputs (all relative to the luxe repo root):
  benchmarks/swebench/subsets/diag_misses_2026_11.json   subset + prior record
  benchmarks/swebench/subsets/raw/verified.jsonl          gold patch, tests
  acceptance/swebench/diag_misses_2026_11/control/<iid>.json
  acceptance/swebench/diag_misses_2026_11/control/harness/...  grading
  ~/.luxe/runs/<run_id>/{events.jsonl,messages.jsonl.gz}

Outputs (next to this script):
  classify.csv   one row per instance (all 46, `resolved` column)
  classify.md    the same table, markdown, plus aggregate counts

Usage: python3 acceptance/swebench/diag_misses_2026_11/classify.py
"""
from __future__ import annotations

import csv
import gzip
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RUNS = Path.home() / ".luxe" / "runs"
SUBSET = ROOT / "benchmarks/swebench/subsets/diag_misses_2026_11.json"
VERIFIED = ROOT / "benchmarks/swebench/subsets/raw/verified.jsonl"
CTRL = HERE / "control"
GRADE = CTRL / "harness/logs/run_evaluation/diag_control"
NEAR = 30  # lines: a model hunk within this many lines of a gold hunk is "near"

WRITE_TOOLS = {"edit_file", "write_file"}
TESTISH = re.compile(r"pytest|python3? -c|runtests|python3? -m unittest|"
                     r"python3? \S+\.py|bin/test|tox\b")
REFUSAL = re.compile(r"^(Shell chain/redirect operators not allowed|"
                     r"Unquoted newline in command|Command '[^']+' not in allowlist)")
DUP = "You already called"
EXIT_ONLY = re.compile(r"^exit code (-?\d+)$")
TEST_PATH = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]*\.py$|_tests?\.py$")


def parse_diff(patch: str) -> dict[str, list[tuple[int, int, str]]]:
    """file -> [(old_start, old_end, header_context)] per hunk."""
    out: dict[str, list[tuple[int, int, str]]] = {}
    cur = None
    for line in (patch or "").splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
            cur = m.group(2) if m else None
            if cur:
                out.setdefault(cur, [])
        elif line.startswith("@@") and cur:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@ ?(.*)", line)
            if m:
                s = int(m.group(1))
                n = int(m.group(2)) if m.group(2) is not None else 1
                out[cur].append((s, s + max(n, 1) - 1, m.group(3).strip()))
    return out


def new_files(patch: str) -> list[str]:
    files, cur = [], None
    for line in (patch or "").splitlines():
        m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
        if m:
            cur = m.group(2)
        elif line.startswith("new file mode") and cur:
            files.append(cur)
    return files


def hunk_relation(model: dict, gold: dict) -> tuple[str, str]:
    """(overlap, proximity). overlap: same|superset|partial|disjoint|none.
    proximity: overlap/near/same_func/far/na over gold source files."""
    mf = {f for f in model if not TEST_PATH.search(f)}
    gf = set(gold)
    if not model:
        return "none", "na"
    if not mf:
        return "tests_only", "na"
    inter = mf & gf
    if not inter:
        ov = "disjoint"
    elif mf == gf:
        ov = "same"
    elif gf <= mf:
        ov = "superset"
    else:
        ov = "partial"
    best = "far" if inter else "na"
    rank = {"na": 0, "far": 1, "same_func": 2, "near": 3, "overlap": 4}
    for f in inter:
        for ms, me, mh in model[f]:
            for gs, ge, gh in gold[f]:
                if ms <= ge and gs <= me:
                    r = "overlap"
                elif min(abs(ms - ge), abs(gs - me)) <= NEAR:
                    r = "near"
                elif mh and gh and mh == gh:
                    r = "same_func"
                else:
                    r = "far"
                if rank[r] > rank[best]:
                    best = r
    return ov, best


def f2p_files(f2p: list[str]) -> set[str]:
    """Best-effort test file/module stems the F2P ids live in."""
    out = set()
    for t in f2p:
        if "::" in t:
            out.add(Path(t.split("::")[0]).stem)
        else:
            m = re.search(r"\(([\w.]+)\)", t)
            if m:  # django: test_x (module.sub.Class)
                parts = m.group(1).split(".")
                out.add(parts[-2] if len(parts) >= 2 else parts[0])
    return out


def load_events(rid: str) -> list[dict]:
    p = RUNS / rid / "events.jsonl"
    if not p.exists():
        return []
    ev = []
    for line in p.read_text().splitlines():
        try:
            ev.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # tolerate a truncated trailing line
    return ev


def load_messages(rid: str) -> list[dict]:
    p = RUNS / rid / "messages.jsonl.gz"
    if not p.exists():
        return []
    msgs = []
    with gzip.open(p, "rt") as fh:
        for line in fh:
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return msgs


def grade(iid: str) -> dict:
    hits = list(GRADE.glob(f"*/{iid}/report.json"))
    if not hits:
        return {}
    rep = json.loads(hits[0].read_text()).get(iid, {})
    ts = rep.get("tests_status", {})
    f2p = ts.get("FAIL_TO_PASS", {})
    p2p = ts.get("PASS_TO_PASS", {})
    return {
        "f2p_pass": len(f2p.get("success", [])),
        "f2p_total": len(f2p.get("success", [])) + len(f2p.get("failure", [])),
        "p2p_fail": len(p2p.get("failure", [])),
        "p2p_total": len(p2p.get("success", [])) + len(p2p.get("failure", [])),
        "patch_applied": rep.get("patch_successfully_applied"),
        "_p2p_fail_ids": p2p.get("failure", []),
    }


def trajectory(rid: str, f2p_stems: set[str]) -> dict:
    ev = load_events(rid)
    msgs = load_messages(rid)
    kinds = [e.get("kind") for e in ev]
    done = next((e for e in ev if e.get("kind") == "single_mode_done"), {})
    steps = [e.get("step", -1) for e in ev if e.get("kind") == "step_usage"]
    calls = [e for e in ev if e.get("kind") == "tool_call"]
    write_steps = [e["step"] for e in calls if e.get("name") in WRITE_TOOLS]
    first_write = min(write_steps) if write_steps else None
    eb = [e["step"] for e in ev if e.get("kind") == "early_bail_fired"]
    exits = [k for k in ("post_write_idle_exit", "habituation_exit",
                         "step_limit_incomplete", "terminal_turn_empty",
                         "terminal_turn_truncated") if k in kinds]

    # Pair bash calls (assistant tool_calls) with their tool results by id.
    results = {m.get("tool_call_id"): m.get("content") or ""
               for m in msgs if m.get("role") == "tool"}
    bash_events = [e for e in calls if e.get("name") == "bash"]
    bash = []
    step_of = []  # step index ~ order of assistant turns
    a_idx = 0
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            if fn.get("name") != "bash":
                continue
            try:
                cmd = json.loads(fn.get("arguments") or "{}").get("command", "")
            except json.JSONDecodeError:
                cmd = fn.get("arguments") or ""
            bash.append((cmd, results.get(tc.get("id"), "")))
            step_of.append(a_idx)
        a_idx += 1
    n_ref = n_dup = n_ok = n_hidden = n_test = n_test_ran = n_f2p = 0
    n_cd = sum(1 for c, _ in bash if c.lstrip().startswith("cd "))
    n_redir = sum(1 for c, _ in bash if re.search(r"2>&1|\|", c))
    test_before = test_after = 0
    for i, (cmd, res) in enumerate(bash):
        refused = bool(REFUSAL.match(res))
        dup = res.startswith(DUP)
        n_ref += refused
        n_dup += dup
        ran = not refused and not dup
        be = bash_events[i] if i < len(bash_events) else {}
        if ran and EXIT_ONLY.match(res.strip()) and be.get("bytes_out", 0) > 0:
            n_hidden += 1  # produced output, model saw only "exit code N"
        elif ran:
            n_ok += 1
        if TESTISH.search(cmd):
            n_test += 1
            if ran:
                n_test_ran += 1
                st = bash_events[i].get("step") if i < len(bash_events) else None
                if first_write is None or (st is not None and st < first_write):
                    test_before += 1
                else:
                    test_after += 1
            if any(s and s in cmd for s in f2p_stems):
                n_f2p += 1
    # What were the 3 calls that ended an idle exit?
    idle_tail = ""
    if "post_write_idle_exit" in kinds:
        idle_tail = ",".join(e.get("name", "?") for e in calls[-3:])
    final_text = ""
    for m in reversed(msgs):
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            final_text = m["content"]
            break
    return {
        "steps": (max(steps) + 1) if steps else 0,
        "tool_calls": done.get("tool_calls_total", len(calls)),
        "aborted": done.get("aborted"),
        "abort_reason": (done.get("abort_reason") or "").replace("\u2014", "-")[:40],
        "exits": "|".join(exits),
        "idle_tail": idle_tail,
        "early_bail_n": len(eb),
        "early_bail_first": min(eb) if eb else "",
        "write_pressure_n": kinds.count("write_pressure_fired"),
        "first_write_step": "" if first_write is None else first_write,
        "writes": len(write_steps),
        "bash_n": len(bash),
        "bash_refused": n_ref,
        "bash_dup": n_dup,
        "bash_cd_prefix": n_cd,
        "bash_pipe_or_2>&1": n_redir,
        "bash_ran_visible": n_ok,
        "bash_ran_output_hidden": n_hidden,
        "testish_cmds": n_test,
        "testish_ran": n_test_ran,
        "testish_ran_before_write": test_before,
        "testish_ran_after_write": test_after,
        "cmd_mentions_f2p_file": n_f2p,
        "final_text_chars": len(final_text),
        "has_messages": bool(msgs),
        "_cmds": " ".join(c for c, _ in bash),
    }


def bucket(r: dict) -> str:
    """Mechanical primary class; C/F split needs reading (see REPORT.md)."""
    if r["resolved"]:
        return "resolved"
    if not r["patch_present"]:
        return "A"
    if r["overlap"] in ("disjoint", "tests_only"):
        return "B"
    if r.get("f2p_total") and r["f2p_pass"] == r["f2p_total"] and r["p2p_fail"]:
        return "D"
    if r.get("patch_applied") is False or r.get("f2p_total", 0) == 0:
        return "E?"
    return "C/F"


def main() -> int:
    sub = json.loads(SUBSET.read_text())
    ids = sub["instance_ids"]
    prior = sub.get("per_instance", {})
    gold = {}
    with VERIFIED.open() as fh:
        for line in fh:
            d = json.loads(line)
            if d["instance_id"] in ids:
                gold[d["instance_id"]] = d
    hs = json.loads((CTRL / "harness/harness_summary.json").read_text())["instances"]
    rows = []
    for iid in ids:
        pred = json.loads((CTRL / f"{iid}.json").read_text())
        g = gold[iid]
        f2p = json.loads(g["FAIL_TO_PASS"]) if isinstance(g["FAIL_TO_PASS"], str) else g["FAIL_TO_PASS"]
        mp = parse_diff(pred.get("model_patch") or "")
        gp = parse_diff(g["patch"])
        ov, prox = hunk_relation(mp, gp)
        r = {
            "iid": iid,
            "prior": prior.get(iid, ""),
            "resolved": bool(hs.get(iid, {}).get("resolved")),
            "patch_present": bool(pred.get("patch_present")),
            "patch_lines": pred.get("patch_lines", 0),
            "rc": pred.get("rc"),
            "wall_s": round(pred.get("wall_s") or 0),
            "gold_files": len(gp),
            "gold_hunks": sum(len(v) for v in gp.values()),
            "model_hunks": sum(len(v) for v in mp.values()),
            "model_files": len(mp),
            "overlap": ov,
            "proximity": prox,
            "model_edits_tests": int(any(TEST_PATH.search(f) for f in mp)),
            "model_new_files": len(new_files(pred.get("model_patch") or "")),
            "f2p_n": len(f2p),
        }
        r.update(grade(iid))
        r.setdefault("f2p_pass", "")
        r.setdefault("f2p_total", "")
        r.setdefault("p2p_fail", "")
        r.setdefault("p2p_total", "")
        r.setdefault("patch_applied", "")
        r.update(trajectory(pred.get("luxe_run_id") or "", f2p_files(f2p)))
        p2p_stems = f2p_files(r.pop("_p2p_fail_ids", []))
        cmds = r.pop("_cmds", "")
        r["cmd_mentions_p2p_fail_file"] = int(any(st and st in cmds for st in p2p_stems))
        r["bucket"] = bucket(r)
        rows.append(r)

    cols = list(rows[0].keys())
    with (HERE / "classify.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    show = ["iid", "bucket", "prior", "patch_lines", "overlap", "proximity",
            "f2p_pass", "f2p_total", "p2p_fail", "model_edits_tests", "steps",
            "gold_hunks", "model_hunks", "exits", "early_bail_first", "first_write_step", "bash_n",
            "bash_refused", "bash_ran_output_hidden", "testish_ran",
            "cmd_mentions_f2p_file", "final_text_chars", "wall_s"]
    lines = ["| " + " | ".join(show) + " |", "|" + "---|" * len(show)]
    for r in sorted(rows, key=lambda x: (x["bucket"], x["iid"])):
        lines.append("| " + " | ".join(str(r.get(c, "")) for c in show) + " |")

    def cnt(pred):
        sel = [r["iid"] for r in rows if pred(r)]
        return len(sel), sel

    miss = lambda r: not r["resolved"]
    agg = {
        "unresolved": cnt(miss),
        "bucket": {b: cnt(lambda r, b=b: r["bucket"] == b)
                   for b in sorted({r["bucket"] for r in rows})},
        "post_write_idle_exit (miss)": cnt(lambda r: miss(r) and "post_write_idle_exit" in r["exits"]),
        "post_write_idle_exit (resolved)": cnt(lambda r: r["resolved"] and "post_write_idle_exit" in r["exits"]),
        "early_bail fired (miss)": cnt(lambda r: miss(r) and r["early_bail_n"]),
        "early_bail fired (resolved)": cnt(lambda r: r["resolved"] and r["early_bail_n"]),
        "any bash refused (miss)": cnt(lambda r: miss(r) and r["bash_refused"]),
        "any test-ish cmd attempted (miss)": cnt(lambda r: miss(r) and r["testish_cmds"]),
        "any test-ish cmd actually ran (miss)": cnt(lambda r: miss(r) and r["testish_ran"]),
        "test-ish ran before first write (miss)": cnt(lambda r: miss(r) and r["testish_ran_before_write"]),
        "test-ish ran after first write (miss)": cnt(lambda r: miss(r) and r["testish_ran_after_write"]),
        "test-ish ran (resolved)": cnt(lambda r: r["resolved"] and r["testish_ran"]),
        "output hidden behind 'exit code N' (all)": cnt(lambda r: r["bash_ran_output_hidden"]),
        "cmd names an F2P test file (miss)": cnt(lambda r: miss(r) and r["cmd_mentions_f2p_file"]),
        "cmd names a file holding a P2P regression (miss)": cnt(lambda r: miss(r) and r["cmd_mentions_p2p_fail_file"]),
        "P2P regressions > 0 (miss)": cnt(lambda r: miss(r) and isinstance(r["p2p_fail"], int) and r["p2p_fail"] > 0),
        "model edits tests (miss)": cnt(lambda r: miss(r) and r["model_edits_tests"]),
        "single write then exit (miss, writes==1)": cnt(lambda r: miss(r) and r["writes"] == 1),
        "single write (resolved, writes==1)": cnt(lambda r: r["resolved"] and r["writes"] == 1),
        "idle exit after 3 consecutive bash calls (miss)": cnt(lambda r: miss(r) and r["idle_tail"] == "bash,bash,bash"),
        "idle exit after 3 consecutive bash calls (resolved)": cnt(lambda r: r["resolved"] and r["idle_tail"] == "bash,bash,bash"),
        "model hunks < gold hunks (miss with patch)": cnt(lambda r: miss(r) and r["patch_present"] and r["model_hunks"] < r["gold_hunks"]),
        "model hunks < gold hunks (resolved)": cnt(lambda r: r["resolved"] and r["model_hunks"] < r["gold_hunks"]),
        "gold is 1 hunk (miss)": cnt(lambda r: miss(r) and r["gold_hunks"] == 1),
        "gold is 1 hunk (resolved)": cnt(lambda r: r["resolved"] and r["gold_hunks"] == 1),
        "no final text (miss)": cnt(lambda r: miss(r) and r["final_text_chars"] == 0),
    }
    sums = {
        "bash calls (all 46)": sum(r["bash_n"] for r in rows),
        "bash refused by validator": sum(r["bash_refused"] for r in rows),
        "bash dedup-refused": sum(r["bash_dup"] for r in rows),
        "bash commands starting with `cd `": sum(r["bash_cd_prefix"] for r in rows),
        "bash commands with a pipe or 2>&1": sum(r["bash_pipe_or_2>&1"] for r in rows),
        "bash ran, output visible": sum(r["bash_ran_visible"] for r in rows),
        "bash ran, output hidden (nonzero exit)": sum(r["bash_ran_output_hidden"] for r in rows),
    }
    out = ["# classify.py output (generated; do not edit)", "",
           "## Aggregates", ""]
    for k, v in agg.items():
        if k == "bucket":
            for b, (n, sel) in v.items():
                out.append(f"- bucket {b}: {n} ({', '.join(sel)})")
        else:
            out.append(f"- {k}: {v[0]} ({', '.join(v[1])})")
    out += ["", "## Bash call totals", ""]
    out += [f"- {k}: {v}" for k, v in sums.items()]
    out += ["", "## Per instance", ""] + lines
    (HERE / "classify.md").write_text("\n".join(out) + "\n")
    print("\n".join(out[:40]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
