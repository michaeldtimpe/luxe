#!/usr/bin/env python3
"""Per-run table + per-arm aggregates for validation_context_ab_2026_11.

Run on m5 from the bench worktree:  python3 <this> [label ...]   (default: champ gemma)
Reads <D>/<label>-<arm>/<variant>/<fixture>/{result,diagnostics}.json + diff.patch
and ~/.luxe/runs/<run_id>/{events.jsonl,synthesizer.md}. Writes runs.json next
to itself and prints the tables. Read-only otherwise.

Measurement limits, stated here so the report does not overclaim:
- tool_call events carry the tool NAME but not its arguments, so a bash call
  cannot be matched to the test command. "test_bash" counts bash calls whose
  output was non-empty (bytes_out>0); refused bash (redirects/chains) has
  bytes_out 0 and is counted separately as bash_refused_or_empty.
- "false claim" is the conservative form: the Checks section says the test
  command ran (or a named check tool ran) and the run has ZERO successful
  calls of that kind. Anything subtler is left to the hand read.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

D = Path(__file__).resolve().parent
RUNS = Path.home() / ".luxe" / "runs"
WRITE = {"write_file", "edit_file"}
EXPLORE = {"read_file", "grep", "bm25_search", "find_symbol", "list_dir", "glob"}
CHECK = {"lint", "typecheck", "lint_js", "typecheck_ts", "lint_rust", "vet_go",
         "git_diff"}
CHECKS_HEAD = re.compile(r"^#{1,6}\s*Checks\b|^\*\*Checks\*\*|^Checks\s*:?\s*$",
                         re.I | re.M)


def patch_id(diff: Path) -> str:
    if not diff.exists() or not diff.read_text(errors="replace").strip():
        return "-"
    p = subprocess.run(["git", "patch-id", "--stable"], input=diff.read_bytes(),
                       capture_output=True)
    return p.stdout.decode().split(" ")[0][:10] or "?"


def checks_section(text: str) -> str:
    m = CHECKS_HEAD.search(text or "")
    return text[m.start():] if m else ""


def one(run_dir: Path, arm: str, variant: str) -> dict:
    res = json.loads((run_dir / "result.json").read_text())
    diag = json.loads((run_dir / "diagnostics.json").read_text())
    rid = diag.get("run_id", "")
    ev_path = RUNS / rid / "events.jsonl"
    ev = [json.loads(ln) for ln in ev_path.read_text().splitlines()] if ev_path.exists() else []
    main_calls = [e for e in ev if e["kind"] == "tool_call" and e.get("phase") == "main"]
    kinds = Counter(e["kind"] for e in ev)
    first_write = next((i for i, e in enumerate(main_calls) if e.get("name") in WRITE), None)
    pre = main_calls if first_write is None else main_calls[:first_write]
    post = [] if first_write is None else main_calls[first_write:]
    done = next((e for e in ev if e["kind"] == "single_mode_done"), {})
    synth = RUNS / rid / "synthesizer.md"
    report = synth.read_text(errors="replace") if synth.exists() else ""
    sect = checks_section(report)
    bash_ok = sum(1 for e in main_calls if e.get("name") == "bash" and e.get("bytes_out", 0) > 0)
    bash_bad = sum(1 for e in main_calls if e.get("name") == "bash" and not e.get("bytes_out", 0))
    checks_used = Counter(e["name"] for e in main_calls if e.get("name") in CHECK)
    false_claims = []
    NEG = re.compile(r"not run|did not|didn't|could not|couldn't|cannot|can't|"
                     r"skipped|not available|unavailable|not applicable|n/a|no test command", re.I)
    POS = re.compile(r"pass|ran\b|\brun\b|\bok\b|clean|✓|succeed|reviewed|"
                     r"no (issues|findings|errors)", re.I)
    for line in sect.splitlines():
        if NEG.search(line) or not POS.search(line):
            continue
        low = line.lower()
        if re.search(r"npm test|pytest|cargo test|go test|make test|\btests?\b", low) \
                and bash_ok == 0 and "test" not in false_claims:
            false_claims.append("test")
        for t in CHECK:
            if re.search(rf"\b{t}\b", low) and not checks_used.get(t) \
                    and t not in false_claims:
                false_claims.append(t)
    inj = next((e for e in ev if e["kind"] == "main_context_injected"), {})
    return {
        "arm": arm, "variant": variant, "fixture": res["fixture_id"], "run_id": rid,
        "score": res.get("score"), "passed": bool(res.get("expected_outcome_passed")),
        "gates": res.get("gates_triggered") or [], "error": res.get("error", ""),
        "adds": res.get("diff_additions", 0), "dels": res.get("diff_deletions", 0),
        "patch_id": patch_id(run_dir / "diff.patch"),
        "wall_s": round(diag.get("wall_s") or 0, 1),
        "prompt_tokens": diag.get("prompt_tokens") or 0,
        "peak_pressure": round(diag.get("peak_context_pressure") or 0, 2),
        "steps_to_first_write": (main_calls[first_write]["step"] if first_write is not None else None),
        "explore_before_write": sum(1 for e in pre if e.get("name") in EXPLORE),
        "calls_total": len(main_calls),
        "verify_calls": sum(checks_used.values()) + bash_ok,
        "verify_after_write": sum(1 for e in post if e.get("name") in CHECK)
                              + sum(1 for e in post if e.get("name") == "bash" and e.get("bytes_out", 0) > 0),
        "checks_used": dict(checks_used), "test_bash": bash_ok,
        "bash_refused_or_empty": bash_bad,
        "compaction_events": sum(v for k, v in kinds.items() if k.startswith("compaction")),
        "pwi_exit": kinds.get("post_write_idle_exit", 0),
        "step_limit": kinds.get("step_limit_incomplete", 0),
        "final_chars": done.get("final_text_chars", 0),
        "checks_section": bool(sect), "false_claims": false_claims,
        "menu_chars": inj.get("menu_chars", 0), "pack_chars": inj.get("pack_chars", 0),
        "injected": bool(inj),
    }


def collect(labels: list[str]) -> list[dict]:
    rows = []
    for label in labels:
        for arm_dir in sorted(D.glob(f"{label}-*")):
            if not arm_dir.is_dir():
                continue
            arm = arm_dir.name[len(label) + 1:]
            for vdir in sorted(arm_dir.glob("mono__*")):
                for fdir in sorted(vdir.iterdir()):
                    if (fdir / "result.json").exists():
                        rows.append({"label": label, **one(fdir, arm, vdir.name)})
    return rows


def summarize(rows: list[dict]) -> None:
    by = defaultdict(list)
    for r in rows:
        by[(r["label"], r["arm"])].append(r)
    hdr = ("label arm n pass wall_sum ptok_M compaction pwi_exit empty_report "
           "checks_sect false_claims verify_runs med_explore med_first_write")
    print(hdr)
    for (label, arm), rs in sorted(by.items()):
        fw = [r["steps_to_first_write"] for r in rs if r["steps_to_first_write"] is not None]
        print(label, arm, len(rs), sum(r["passed"] for r in rs),
              round(sum(r["wall_s"] for r in rs)),
              round(sum(r["prompt_tokens"] for r in rs) / 1e6, 2),
              sum(r["compaction_events"] for r in rs),
              sum(r["pwi_exit"] for r in rs),
              sum(1 for r in rs if not r["final_chars"]),
              sum(r["checks_section"] for r in rs),
              sum(bool(r["false_claims"]) for r in rs),
              sum(1 for r in rs if r["verify_calls"] > 0),
              median([r["explore_before_write"] for r in rs]) if rs else "-",
              median(fw) if fw else "-")
    print()
    print("per fixture: label fixture arm -> pass/3 | patch_ids | verify_calls")
    fx = defaultdict(list)
    for r in rows:
        fx[(r["label"], r["fixture"], r["arm"])].append(r)
    for (label, f, arm), rs in sorted(fx.items()):
        print(f"{label:6} {f:44} {arm:5} {sum(r['passed'] for r in rs)}/{len(rs)} "
              f"{','.join(r['patch_id'] for r in rs):34} "
              f"{[r['verify_calls'] for r in rs]} pwi={sum(r['pwi_exit'] for r in rs)}")


if __name__ == "__main__":
    labels = sys.argv[1:] or ["champ", "gemma"]
    rows = collect(labels)
    (D / "runs.json").write_text(json.dumps(rows, indent=1))
    summarize(rows)
