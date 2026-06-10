#!/usr/bin/env python
"""Track A — OFFLINE recovery analysis for gitaudit deep-mode "unparsed" chunks.

Experiment plan: ~/.claude/plans/elegant-painting-mccarthy.md

The gitaudit sweep flagged ~56 chunks across repos into `unparsed_chunks` (no usable
note recovered) even though their raw `chunk-NN.md` dumps often contain a clean,
ENUMERATED findings list — the champion rambled and never emitted the required
`# Repository audit` header, so `extract_report` couldn't slice it and the strict
`deep._heuristic_findings` (which demands an inline severity word) salvaged 0-1 lines.

This script replays the ALREADY-CAPTURED unparsed dumps (zero model cost for R1)
through the current heuristic vs an IMPROVED heuristic, and reports the salvage rate
— i.e. how many "failures" are recoverable serialization failures vs truly-lost
(empty/no-output) reasoning failures. Read-only: reads ~/.luxe/reports/.../*.work
dumps + writes scripts/out/recover_offline.csv. Nothing in the package changes.

Usage:
  uv run python scripts/recover_offline.py            # R1, all gap repos
  uv run python scripts/recover_offline.py --sample 3 # also print N recovered samples/repo
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
from pathlib import Path

from luxe.gitkit import deep, store

OUT = Path(__file__).resolve().parent / "out" / "recover_offline.csv"

# Repos whose latest gitaudit .work has unparsed chunks (the gap corpus).
GAP_REPOS = ["deluxe", "neo-llm-bench", "nothing-ever-happens", "ane-router",
             "yet-another-statusline", "stockton", "whetstone", "luxe", "aurora"]

_CLONE_ROOT = Path.home() / ".luxe" / "sweep-clones"


def _find(name: str) -> Path | None:
    for b in [Path("~/Downloads").expanduser() / name, _CLONE_ROOT / name]:
        if (b / ".git").exists():
            return b
    return None


# --- improved heuristic (R1) ------------------------------------------------
# The dumps express findings as NUMBERED BOLD list items carrying a file/line or
# code reference, e.g.:
#   "1. **cli.py line 29-38**: `git clone --depth=1` without verifying the URL scheme"
#   "2. **Line 1288**: `tempfile.mkdtemp(...)` — temp dir only cleaned up if ..."
#   "3. **`_check_regex_present` (line 568-649)**: bug on line 609 ..."
# plus the canonical report bullets (**File:** / **Impact:** / severity words).
# Match those shapes; dedupe; cap. Deliberately keyed on the FINDING shape (numbered
# + bold lead, or a report bullet) so plain exploration narrative ("Let me look at
# cli.py:29") is NOT swept in.
_NUM_BOLD = re.compile(r"^\s*\d+[.)]\s+\*\*")
_REPORT_BULLET = re.compile(
    r"\*\*\s*(file|issue|bug|severity|line|impact|fix|problem|risk|location)\b", re.I)
_FILE_LINE = re.compile(r"\b[\w./-]+\.(py|rs|js|ts|tsx|go|sh|ya?ml|toml|c|cpp|h)\b"
                        r"(?:[:\s]+(?:line\s+)?\d+)", re.I)
_BOLD_FILE = re.compile(r"\*\*[^*]*?(?:\.(py|rs|js|ts|go|sh|ya?ml)\b|line\s+\d+)[^*]*?\*\*",
                        re.I)


def improved_findings(text: str, *, cap: int = 60) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if len(s) < 12:
            continue
        is_finding = (
            (_NUM_BOLD.search(s) and (_FILE_LINE.search(s) or _BOLD_FILE.search(s)
                                      or "`" in s))
            or _REPORT_BULLET.search(s)
            or deep._SEV_LINE_RE.search(s)
        )
        if not is_finding:
            continue
        key = re.sub(r"\s+", " ", s.lower())[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(s[:200])
        if len(out) >= cap:
            break
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=2,
                    help="print N recovered-finding samples per repo (eyeball quality)")
    args = ap.parse_args()
    OUT.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for name in GAP_REPOS:
        p = _find(name)
        if not p:
            print(f"  {name}: (no local clone) — skip")
            continue
        rd = store.reports_dir(str(p))
        works = [w for w in sorted(glob.glob(str(rd / "gitaudit-*.work")),
                                   key=os.path.getmtime)
                 if (Path(w) / "xref.json").is_file()]  # skip interrupted runs
        if not works:
            continue
        w = Path(works[-1])
        xref = json.loads((w / "xref.json").read_text())
        unp = xref.get("unparsed_chunks", [])
        samples_left = args.sample
        for u in unp:
            m = re.match(r"chunk (\d+)", u)
            if not m:
                continue
            idx = int(m.group(1))
            f = w / f"chunk-{idx:02d}.md"
            text = f.read_text() if f.is_file() else ""
            cur = deep._heuristic_findings(text)
            imp = improved_findings(text)
            empty = len(text.strip()) < 40
            rows.append({
                "repo": name, "chunk": idx, "out_chars": len(text),
                "empty": int(empty), "current_findings": len(cur),
                "improved_findings": len(imp),
                "recovered": int(len(imp) >= 1 and len(cur) == 0),
                "salvageable": int(len(imp) >= 1),
            })
            if samples_left > 0 and imp and len(cur) == 0:
                print(f"  [{name} chunk {idx}] recovered {len(imp)} (was {len(cur)}); e.g.:")
                for s in imp[:2]:
                    print("      •", s[:110])
                samples_left -= 1

    if not rows:
        print("no unparsed-chunk dumps found in the gap corpus — nothing to write")
        return
    with OUT.open("w", newline="") as fh:
        wri = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        wri.writeheader()
        wri.writerows(rows)

    n = len(rows)
    empty = sum(r["empty"] for r in rows)
    salvageable = sum(r["salvageable"] for r in rows)
    newly = sum(r["recovered"] for r in rows)
    truly_lost = sum(1 for r in rows if r["salvageable"] == 0)
    print("\n=== Track A (R1, heuristic, 0 model calls) ===")
    print(f"unparsed dumps analyzed: {n}")
    print(f"  empty/(no output):     {empty}  (truly unrecoverable)")
    print(f"  salvageable (improved>=1 finding): {salvageable}  ({100*salvageable/n:.0f}%)")
    print(f"  NEWLY recovered (current=0 -> improved>=1): {newly}")
    print(f"  truly lost (improved=0): {truly_lost}  (incl. {empty} empty)")
    print(f"  current heuristic salvaged: {sum(1 for r in rows if r['current_findings']>=1)}")
    print(f"\nCSV -> {OUT}")


if __name__ == "__main__":
    main()
