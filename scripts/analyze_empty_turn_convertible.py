"""Phase 0 grounding (read-only): characterize the multi_turn `empty_turn` corpus.

Reflection-cycle Track 1 gate input. For each stored miss_func / miss_param record we
classify every `empty_turn_model_response` failure into honest buckets so Phase 1's
detection-rate is measured against the RIGHT denominator (genuine give-ups), not the
naive "all empty_turn fails" set.

The subtlety this script exists to expose (found during implementation, 2026-05-24):
the miss_* `empty_turn` is the REVEAL turn — a (usually message-less) turn where a
withheld function is injected and an earlier request is meant to be fulfilled. But the
model often already answered that request EARLY via alternative tools, so its empty
reveal-turn is "nothing left to do," not a give-up; the benchmark fails it on a
state-checker path mismatch a verify-repair pass cannot and should not fix. Those
`alt_completion` cases must be separated from genuine give-ups.

Buckets per empty_turn failure:
  - genuine_giveup : needed fn available at the fail turn AND the model did NOT
                     over-act before it (no early/alt completion signal). The verify-
                     repair TARGET; Phase 1 detection is measured here.
  - alt_completion : the model acted on a pre-reveal turn where GT expected nothing
                     (it answered early via alternatives). Verify SHOULD abstain here
                     (treat like a pass for the false-gap reading).
  - fn_unavailable : the GT-needed fn for the fail turn was not exposed (off-nominal).

Also emits the true-PASS corpus (state-checker passes) for the Phase 1 false-gap sample.

Usage:
    .venv/bin/python -m scripts.analyze_empty_turn_convertible \
        [--rep acceptance/bfcl/multi_turn_miss_func/m5_rep_1/multi_turn_miss_func] \
        [--rep acceptance/bfcl/multi_turn_miss_param/m5_rep_1/multi_turn_miss_param] \
        [--out acceptance/bfcl/reflect_phase0/convertible_manifest.json]

Read-only except for the manifest JSON (under gitignored acceptance/).
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BFCL_DATA = Path(os.environ.get("LUXE_BFCL_DATA_DIR", Path.home() / ".luxe" / "bfcl-data"))

_TURN_RE = re.compile(r"turn (\d+)")


def _load_jsonl_by_id(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                d = json.loads(line)
                out[d["id"]] = d
    return out


def _category_of(rep_dir: Path) -> str:
    # rep dir leaf is the category (…/multi_turn_miss_func/m5_rep_1/multi_turn_miss_func)
    return rep_dir.name


def _failed_turn(record: dict[str, Any]) -> int | None:
    msg = (record.get("checker") or {}).get("error_message") or ""
    m = _TURN_RE.search(msg)
    return int(m.group(1)) if m else None


def _gt_call_names(gt_turn: list[Any]) -> list[str]:
    """Function names referenced in a GT turn's call-strings (e.g. "mean(...)")."""
    names: list[str] = []
    for call in gt_turn or []:
        if isinstance(call, str):
            n = call.split("(", 1)[0].strip()
            if n:
                names.append(n)
    return names


def classify_record(
    record: dict[str, Any],
    question: list[Any],
    gt: list[Any],
) -> dict[str, Any]:
    """Classify one record. Returns a dict with bucket + the objective signals."""
    pid = record.get("id", "?")
    reason = record.get("reason", "") or ""
    passed = bool(record.get("passed"))
    out: dict[str, Any] = {"id": pid, "passed": passed, "reason": reason}

    if passed:
        out["bucket"] = "pass"
        return out
    if not reason.endswith("empty_turn_model_response"):
        out["bucket"] = "other_fail"
        return out

    ft = _failed_turn(record)
    out["failed_turn"] = ft
    if ft is None:
        out["bucket"] = "empty_turn_unparsed"
        return out

    exposed = record.get("exposed_tool_names") or []
    exposed_at_ft = set(exposed[ft]) if ft < len(exposed) else set()
    needed = _gt_call_names(gt[ft] if ft < len(gt) else [])
    fn_available = bool(needed) and all(n in exposed_at_ft for n in needed)
    out["needed_fns"] = needed
    out["fn_available"] = fn_available

    # Empty (message-less) reveal turn vs an explicit ignored request?
    ft_turn = question[ft] if ft < len(question) else []
    has_user_msg = bool(ft_turn) and any(
        isinstance(m, dict) and m.get("role") == "user" for m in ft_turn
    )
    out["fail_turn_has_user_msg"] = has_user_msg

    # Over-action before the reveal: model acted on a pre-fail turn where GT wanted
    # nothing → it likely answered early via alternatives (alt_completion signal).
    decoded = record.get("decoded_turns") or []
    over_acted = False
    for k in range(min(ft, len(decoded), len(gt))):
        model_acted = any(len(step) > 0 for step in decoded[k])
        gt_empty = len(gt[k] or []) == 0
        if model_acted and gt_empty:
            over_acted = True
            break
    out["over_acted_pre_reveal"] = over_acted

    if not fn_available:
        out["bucket"] = "fn_unavailable"
    elif over_acted:
        out["bucket"] = "alt_completion"
    else:
        out["bucket"] = "genuine_giveup"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rep", action="append", default=[],
                    help="Per-category record dir. Repeatable. Defaults to the two miss_* m5 reps.")
    ap.add_argument("--out", type=Path,
                    default=ROOT / "acceptance" / "bfcl" / "reflect_phase0" / "convertible_manifest.json")
    args = ap.parse_args()

    reps = args.rep or [
        "acceptance/bfcl/multi_turn_miss_func/m5_rep_1/multi_turn_miss_func",
        "acceptance/bfcl/multi_turn_miss_param/m5_rep_1/multi_turn_miss_param",
    ]

    manifest: dict[str, Any] = {"categories": {}}
    print(f"{'category':28s} {'n':>4} {'pass':>5} {'empty':>6} {'genuine':>8} {'alt':>4} {'fn_un':>6}")
    print("-" * 70)

    for rep in reps:
        rep_dir = (ROOT / rep) if not Path(rep).is_absolute() else Path(rep)
        category = _category_of(rep_dir)
        src = _load_jsonl_by_id(BFCL_DATA / f"BFCL_v4_{category}.json")
        ans = _load_jsonl_by_id(BFCL_DATA / "possible_answer" / f"BFCL_v4_{category}.json")
        if not rep_dir.is_dir():
            print(f"{category:28s}  (rep dir not found: {rep_dir})")
            continue

        buckets: dict[str, list[dict[str, Any]]] = {}
        n = 0
        for jf in sorted(rep_dir.glob("*.json")):
            record = json.loads(jf.read_text())
            pid = record.get("id")
            q = (src.get(pid) or {}).get("question", [])
            gt = (ans.get(pid) or {}).get("ground_truth", [])
            res = classify_record(record, q, gt)
            buckets.setdefault(res["bucket"], []).append(res)
            n += 1

        def c(b: str) -> int:
            return len(buckets.get(b, []))

        empty_total = c("genuine_giveup") + c("alt_completion") + c("fn_unavailable") + c("empty_turn_unparsed")
        manifest["categories"][category] = {
            "n": n,
            "pass_ids": [r["id"] for r in buckets.get("pass", [])],
            "genuine_giveup_ids": [r["id"] for r in buckets.get("genuine_giveup", [])],
            "alt_completion_ids": [r["id"] for r in buckets.get("alt_completion", [])],
            "fn_unavailable_ids": [r["id"] for r in buckets.get("fn_unavailable", [])],
            "other_fail": c("other_fail"),
            "detail": {r["id"]: r for b in buckets.values() for r in b
                       if r["bucket"] in ("genuine_giveup", "alt_completion", "fn_unavailable")},
        }
        print(f"{category:28s} {n:>4} {c('pass'):>5} {empty_total:>6} "
              f"{c('genuine_giveup'):>8} {c('alt_completion'):>4} {c('fn_unavailable'):>6}")

    # Roll-up
    g = sum(len(v["genuine_giveup_ids"]) for v in manifest["categories"].values())
    a = sum(len(v["alt_completion_ids"]) for v in manifest["categories"].values())
    fu = sum(len(v["fn_unavailable_ids"]) for v in manifest["categories"].values())
    p = sum(len(v["pass_ids"]) for v in manifest["categories"].values())
    manifest["rollup"] = {"genuine_giveup": g, "alt_completion": a, "fn_unavailable": fu, "pass": p}
    print("-" * 70)
    print(f"ROLLUP genuine_giveup={g}  alt_completion={a}  fn_unavailable={fu}  pass={p}")
    print(f"\nThe Plan-agent's grounding claimed ~41 convertible. Genuine (verify-targetable) "
          f"give-ups after separating alt-completions = {g}.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2))
    print(f"\nmanifest → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
