"""Dump all 58 multi_turn empty_turn failures in a compact, readable form so the
genuine-giveup vs alt-completion call can be made by reading the actual work (the
structural over_acted heuristic proved unreliable — it mislabels give-ups where the
model acted on some earlier turn but still abandoned the key ask).

Per problem: the user's asks, what the assistant did/said, the failed turn + the GT
action it was supposed to take there, and the prior heuristic label (for comparison).

Read-only. Usage:  .venv/bin/python -m scripts.dump_empty_turn_for_labeling > /tmp/label58.txt
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BD = Path(os.environ.get("LUXE_BFCL_DATA_DIR", Path.home() / ".luxe" / "bfcl-data"))
from luxe.agents import reflect as R  # noqa: E402

_REP = {
    "multi_turn_miss_func": "acceptance/bfcl/multi_turn_miss_func/m5_rep_1/multi_turn_miss_func",
    "multi_turn_miss_param": "acceptance/bfcl/multi_turn_miss_param/m5_rep_1/multi_turn_miss_param",
}
_TURN = re.compile(r"turn (\d+)")


def _src(cat: str) -> dict:
    out = {}
    for line in open(BD / f"BFCL_v4_{cat}.json"):
        d = json.loads(line); out[d["id"]] = d
    return out


def _ans(cat: str) -> dict:
    out = {}
    for line in open(BD / "possible_answer" / f"BFCL_v4_{cat}.json"):
        d = json.loads(line); out[d["id"]] = d.get("ground_truth", [])
    return out


def main() -> int:
    man = json.loads((ROOT / "acceptance/bfcl/reflect_phase0/convertible_manifest.json").read_text())
    for cat, rep in _REP.items():
        src, ans = _src(cat), _ans(cat)
        info = man["categories"][cat]
        heur = {pid: "genuine" for pid in info["genuine_giveup_ids"]}
        heur.update({pid: "alt" for pid in info["alt_completion_ids"]})
        heur.update({pid: "fn_unavail" for pid in info["fn_unavailable_ids"]})
        empties = sorted(heur, key=lambda p: int(p.rsplit("_", 1)[1]))
        print(f"\n{'#'*78}\n# {cat}: {len(empties)} empty_turn failures\n{'#'*78}")
        for pid in empties:
            rec = json.loads((ROOT / rep / f"{pid}.json").read_text())
            ck = rec.get("checker", {}).get("error_message", "")
            m = _TURN.search(ck); ft = int(m.group(1)) if m else -1
            gt = ans.get(pid, [])
            gt_at = gt[ft] if 0 <= ft < len(gt) else []
            asks = [str(m.get("content") or "").strip()
                    for m in rec.get("transcript", []) if m.get("role") == "user"]
            actions = [c for turn in rec.get("decoded_turns", []) for step in turn for c in step]
            prose = [str(m.get("content") or "").strip()
                     for m in rec.get("transcript", [])
                     if m.get("role") == "assistant" and str(m.get("content") or "").strip()]
            last = (prose[-1][:200] + "…") if prose else ""
            print(f"\n----- {pid} [heur={heur[pid]}] ft={ft} | GT@ft={gt_at}")
            print(f"  ASKS: " + " || ".join(a[:90] for a in asks))
            print(f"  ACTIONS({len(actions)}): {actions}")
            print(f"  LASTPROSE: {last!r}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
