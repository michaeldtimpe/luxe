"""Dump all 58 multi_turn empty_turn failures in a compact, readable form so the
genuine-giveup vs alt-completion call can be made by reading the actual work (the
structural over_acted heuristic proved unreliable — it mislabels give-ups where the
model acted on some earlier turn but still abandoned the key ask).

Per problem: the user's asks, what the assistant did/said, the failed turn + the GT
action it was supposed to take there, the hand label (effective = reviewed_label or label)
+ confidence + note, and the SAVED verify verdict (gap + per-deficiency specificity) read
from the Phase-1 result file — so the give-up call can be checked beside why verify flagged
or abstained, with NO oMLX re-run. (Phase 1 persisted gap/ok/specificity, not the
deficiency free-text, so the verdict line shows the specificity tags.)

Read-only. Usage:
    .venv/bin/python -m scripts.dump_empty_turn_for_labeling > /tmp/label58.txt
    .venv/bin/python -m scripts.dump_empty_turn_for_labeling --only-borderline > /tmp/borderline14.txt

`--only-borderline` keeps only labels with confidence != "clear" (the 14 pending the user
spot-check).
"""

from __future__ import annotations

import argparse
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
_LABELS = "acceptance/bfcl/reflect_phase0/giveup_labels.json"
_VERDICTS = "acceptance/bfcl/reflect_phase1/verify_only_result.json"


def _eff_label(v: dict | None) -> str:
    """Effective label for display: a spot-check `reviewed_label` overrides the original
    `label` (provenance preserved — the original stays in the JSON)."""
    v = v or {}
    return v.get("reviewed_label") or v.get("label") or "?"


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
    ap = argparse.ArgumentParser(description="Dump multi_turn empty_turn failures for labeling.")
    ap.add_argument("--only-borderline", action="store_true",
                    help='Keep only labels with confidence != "clear" (the 14 pending the '
                         "user spot-check); default prints all 58 empty_turn failures.")
    ap.add_argument("--labels", type=Path, default=ROOT / _LABELS)
    ap.add_argument("--verdicts", type=Path, default=ROOT / _VERDICTS,
                    help="Phase-1 result file; its 'verdicts' block is printed per pid.")
    args = ap.parse_args()

    man = json.loads((ROOT / "acceptance/bfcl/reflect_phase0/convertible_manifest.json").read_text())
    labels = json.loads(args.labels.read_text())["labels"]
    # Saved verify verdicts (gap + per-deficiency specificity). Resilient: if the Phase-1
    # result file is absent, fall back to an empty map and WARN per pid (never crash).
    if args.verdicts.is_file():
        verdicts = json.loads(args.verdicts.read_text()).get("verdicts", {})
    else:
        print(f"# WARN: verdicts file not found: {args.verdicts} (verdict lines will warn)")
        verdicts = {}

    for cat, rep in _REP.items():
        ans = _ans(cat)
        info = man["categories"][cat]
        heur = {pid: "genuine" for pid in info["genuine_giveup_ids"]}
        heur.update({pid: "alt" for pid in info["alt_completion_ids"]})
        heur.update({pid: "fn_unavail" for pid in info["fn_unavailable_ids"]})
        empties = sorted(heur, key=lambda p: int(p.rsplit("_", 1)[1]))
        if args.only_borderline:
            empties = [p for p in empties
                       if (labels.get(p) or {}).get("confidence") != "clear"]
        kind = "borderline" if args.only_borderline else "empty_turn"
        print(f"\n{'#'*78}\n# {cat}: {len(empties)} {kind} failures\n{'#'*78}")
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
            lab = labels.get(pid) or {}
            print(f"\n----- {pid} [label={_eff_label(lab)} conf={lab.get('confidence', '?')} "
                  f"heur={heur[pid]}] ft={ft} | GT@ft={gt_at}")
            if lab.get("note"):
                print(f"  NOTE: {lab['note']}")
            if lab.get("reviewed_label"):
                print(f"  REVIEW: reviewed_label={lab['reviewed_label']!r} "
                      f"review_note={lab.get('review_note', '')!r} (orig label={lab.get('label')!r})")
            print(f"  ASKS: " + " || ".join(a[:90] for a in asks))
            print(f"  ACTIONS({len(actions)}): {actions}")
            print(f"  LASTPROSE: {last!r}")
            v = verdicts.get(pid)
            if v is None:
                print(f"  VERIFY: WARN no saved verdict for {pid}")
            else:
                print(f"  VERIFY: gap={v.get('gap')} ok={v.get('ok')} "
                      f"deficiencies(specificity)={v.get('specificity')}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
