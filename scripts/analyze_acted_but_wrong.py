"""WS2 sizing pass (READ-ONLY): characterize the multi_turn "acted-but-wrong" corpus.

The reflect cycle hand-labeled the 58 `empty_turn` GIVE-UPS (zero-call turns) and Phase 2
repair already targets that mass (→ HOLD). This script sizes the DISJOINT, never-examined
slice: failures where the model ACTED but the final state/response was wrong
(`instance_state_mismatch` + `execution_response_mismatch`). The question it answers is how
much of that mass is genuine WRONG-BINDING (right tool, wrong argument value — wrong
recipient/number/format) vs acceptable path-divergence / state-checker rigidity — so any
future go/no-go on an intervention is data-grounded.

*** SIZING HEURISTIC — NOT GROUND TRUTH. ***
BFCL grades final STATE, not exact calls, and luxe's vendored GT is a single canonical
call-string per arg (no accepted-value lists). So `gt_value_mismatch` OVER-counts: some
flagged args still pass the state checker (defaults, benign normalization, equivalent
routes). Treat the counts as an upper bound and confirm via the `--dump` skim — same
posture as the Phase-0 `over_acted` heuristic that hand-labeling had to correct.

Method (adapted to the data, deviating from the plan's single-failed-turn approach):
`instance_state_mismatch` error messages carry NO turn index, and real wrong-bindings are
often turn-shifted (e.g. miss_func_33 sends to the wrong recipient one turn early). So we
diff over the WHOLE conversation: flatten all model calls (`decoded_turns`) and all GT calls
(`ground_truth`), group by function name, align instances greedily by max GT-arg overlap
(deterministic; subsumes exact multiset match), then compare ONLY the args GT specifies
(model-added args — likely defaults — are ignored). Turn counts are index-aligned (verified:
71/71 equal in miss_func), so a `turn_shifted` flag is recorded as color.

Buckets (one primary per failure, by precedence):
  gt_value_mismatch ("wrong_value")  : ≥1 matched call has a GT arg with a different value
  omission ("omission_at_turn")      : a GT function was never called anywhere (model acted otherwise)
  extra_action                       : model called a function with no GT counterpart
  normalization_uncertain            : only-ambiguous diffs (nested-container ordering / un-coercible)
  path_divergence                    : every GT call matched with equal args, yet state failed → ordering/elsewhere/checker rigidity
  unparsed                           : GT calls could not be parsed (can't assess)

Usage:
    .venv/bin/python -m scripts.analyze_acted_but_wrong            # summary + manifest
    .venv/bin/python -m scripts.analyze_acted_but_wrong --dump --bucket gt_value_mismatch --sample 20
Read-only except the manifest JSON (under gitignored acceptance/).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BFCL_DATA = Path(os.environ.get("LUXE_BFCL_DATA_DIR", Path.home() / ".luxe" / "bfcl-data"))

_ACTED_WRONG = ("instance_state_mismatch", "execution_response_mismatch")
_ID_HINT = re.compile(r"(?:^|_)(id|ids|receiver|recipient|sender|user|to|card|ticket|account)s?$", re.I)
_ID_VALUE = re.compile(r"^[A-Za-z]{2,}\d{2,}$")  # e.g. USR003, card_… handled by name too
_CAVEAT = ("*** SIZING HEURISTIC — not ground truth; gt_value_mismatch OVER-counts (BFCL grades "
           "final state, not exact calls; GT is a single canonical call-string) — confirm via --dump skim ***")

_MISSING = object()


# --- parsing + normalization (the unit-testable core) ------------------------

def parse_call(call_str: str) -> tuple[str, dict[str, Any]] | None:
    """Parse a BFCL call-string `name(a=…, b=…)` into `(name, {arg: value})`.

    Returns None on ANY failure (non-literal value, enum/constant, positional-only,
    duplicate kwarg, malformed) — callers route None to the `unparsed` bucket. The
    exception is caught HERE, per-call, so a single bad call never aborts the run.
    """
    try:
        node = ast.parse(call_str.strip(), mode="eval").body
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            return None
        name = node.func.id
        if node.args:  # positional args: BFCL call-strings are all-keyword; bail to unparsed
            return None
        args: dict[str, Any] = {}
        for kw in node.keywords:
            if kw.arg is None:  # **kwargs splat
                return None
            args[kw.arg] = ast.literal_eval(kw.value)  # raises on non-literal → caught below
        return name, args
    except Exception:  # noqa: BLE001 — any parse/eval failure → unparsed
        return None


def norm_eq(model_val: Any, gt_val: Any) -> bool | None:
    """Compare a model arg value to the GT value under light normalization.

    Returns True (match), False (clear scalar mismatch → gt_value_mismatch), or None
    (ambiguous → normalization_uncertain). None is reserved for differing nested
    containers (list/dict), where ordering/structure differences are not reliably a
    behavioral error.
    """
    if model_val == gt_val:
        return True
    if isinstance(model_val, (list, dict, tuple)) or isinstance(gt_val, (list, dict, tuple)):
        return None  # container mismatch — too noisy to call wrong; defer to skim
    # numeric coercion: "500" vs 500, 500 vs 500.0
    try:
        if float(model_val) == float(gt_val):
            return True
    except (TypeError, ValueError):
        pass
    # whitespace-only / str-cast equality: 'def ' vs 'def', 0 vs '0'
    if str(model_val).strip() == str(gt_val).strip():
        return True
    return False


def param_subtype(name: str, gt_val: Any) -> str:
    """Coarse sub-tag for a mismatched param: recipient/ID-like · numeric · string/format."""
    if _ID_HINT.search(name) or (isinstance(gt_val, str) and _ID_VALUE.match(gt_val)):
        return "recipient_id"
    if isinstance(gt_val, (int, float)) and not isinstance(gt_val, bool):
        return "numeric"
    return "string_format"


# --- whole-conversation matching + classification ----------------------------

def _flatten(turns: list[Any], is_model: bool) -> tuple[list[tuple[str, dict[str, Any], int]], int]:
    """Flatten per-turn calls to ([(name, args, turn_idx)], n_unparsed), skipping unparseables.

    model `decoded_turns` is [turn][step][call_str]; GT `ground_truth` is [turn][call_str].
    """
    out: list[tuple[str, dict[str, Any], int]] = []
    out_unparsed = 0
    for t, turn in enumerate(turns or []):
        calls = ([c for step in turn for c in step] if is_model else list(turn or []))
        for cs in calls:
            p = parse_call(cs) if isinstance(cs, str) else None
            if p is None:
                out_unparsed += 1
            else:
                out.append((p[0], p[1], t))
    return out, out_unparsed


def classify_failure(model_turns: list[Any], gt_turns: list[Any]) -> dict[str, Any]:
    """Diff a single acted-but-wrong failure over the whole conversation. See module docstring."""
    model, m_unparsed = _flatten(model_turns, is_model=True)
    gt, g_unparsed = _flatten(gt_turns, is_model=False)

    model_by: dict[str, list[tuple[dict[str, Any], int]]] = defaultdict(list)
    for nm, args, t in model:
        model_by[nm].append((args, t))

    mismatches: list[dict[str, Any]] = []
    omissions: list[str] = []
    uncertain: list[dict[str, Any]] = []
    used: set[tuple[str, int]] = set()
    matched: dict[int, tuple[str, int, int]] = {}  # gt_idx → (name, model_idx, model_turn)
    turn_shifted = False

    def _overlap(gargs: dict[str, Any], margs: dict[str, Any]) -> int:
        return sum(1 for k, v in gargs.items() if k in margs and norm_eq(margs[k], v) is True)

    # Pass 1 — exact: a GT call binds to an unused same-name model call iff EVERY GT arg is
    # present and equal. This claims the unambiguous matches first (so send_message(Bob) takes
    # the model's send_message(Bob), leaving a stray GT send_message(Alice) to fall through).
    for gi, (nm, gargs, _gt_t) in enumerate(gt):
        for i, (margs, mt) in enumerate(model_by.get(nm, [])):
            if (nm, i) in used:
                continue
            if all(k in margs and norm_eq(margs[k], v) is True for k, v in gargs.items()):
                used.add((nm, i)); matched[gi] = (nm, i, mt); break

    # Pass 2 — fuzzy: for still-unmatched GT calls, take the unused same-name candidate with
    # max arg-overlap, but ONLY if there's positive evidence it's the same intended call
    # (overlap>0) or it's the sole candidate (forced — covers single-arg fns like
    # add_to_watchlist(stock=…)). Otherwise it's a genuine omission, NOT a mis-aligned mismatch.
    for gi, (nm, gargs, _gt_t) in enumerate(gt):
        if gi in matched:
            continue
        cands = [i for i in range(len(model_by.get(nm, []))) if (nm, i) not in used]
        if not cands:
            omissions.append(nm)
            continue
        best_i = max(cands, key=lambda i: _overlap(gargs, model_by[nm][i][0]))
        if _overlap(gargs, model_by[nm][best_i][0]) > 0 or len(cands) == 1:
            used.add((nm, best_i)); matched[gi] = (nm, best_i, model_by[nm][best_i][1])
        else:
            omissions.append(nm)

    # Diff each matched pair over ONLY the args GT specifies (model-added args, likely defaults,
    # are ignored — sidesteps the no-tool-schema defaults problem).
    for gi, (nm, mi, mt) in matched.items():
        gargs, gt_t = gt[gi][1], gt[gi][2]
        margs = model_by[nm][mi][0]
        if mt != gt_t:
            turn_shifted = True
        for k, gv in gargs.items():
            mv = margs.get(k, _MISSING)
            if mv is _MISSING:
                mismatches.append({"fn": nm, "param": k, "model": None, "gt": gv,
                                   "subtype": param_subtype(k, gv), "kind": "omitted_arg"})
                continue
            eq = norm_eq(mv, gv)
            if eq is True:
                continue
            rec = {"fn": nm, "param": k, "model": mv, "gt": gv, "subtype": param_subtype(k, gv)}
            (mismatches if eq is False else uncertain).append(rec)

    extras = [nm for nm, insts in model_by.items() for i in range(len(insts)) if (nm, i) not in used]

    if not gt and g_unparsed:
        bucket = "unparsed"
    elif mismatches:
        bucket = "gt_value_mismatch"
    elif omissions:
        bucket = "omission"
    elif extras:
        bucket = "extra_action"
    elif uncertain:
        bucket = "normalization_uncertain"
    else:
        bucket = "path_divergence"

    return {"bucket": bucket, "mismatches": mismatches, "omissions": omissions,
            "extras": extras, "uncertain": uncertain, "turn_shifted": turn_shifted,
            "n_unparsed": m_unparsed + g_unparsed, "n_gt": len(gt), "n_model": len(model)}


# --- I/O + driver ------------------------------------------------------------

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


def _is_acted_wrong(record: dict[str, Any]) -> bool:
    reason = record.get("reason") or ""
    return (not record.get("passed")) and reason.endswith(_ACTED_WRONG)


def _user_asks(record: dict[str, Any]) -> list[str]:
    return [str(m.get("content") or "").strip()
            for m in record.get("transcript", []) if m.get("role") == "user"]


def _iter_failures(rep_dir: Path, ans: dict[str, dict[str, Any]]):
    for jf in sorted(rep_dir.glob("*.json"), key=lambda p: int(p.stem.rsplit("_", 1)[1])):
        rec = json.loads(jf.read_text())
        if not _is_acted_wrong(rec):
            continue
        gt = (ans.get(rec.get("id")) or {}).get("ground_truth", [])
        res = classify_failure(rec.get("decoded_turns", []), gt)
        yield rec, res


def _default_reps() -> list[str]:
    return ["acceptance/bfcl/multi_turn_miss_func/m5_rep_1/multi_turn_miss_func",
            "acceptance/bfcl/multi_turn_miss_param/m5_rep_1/multi_turn_miss_param"]


def _run_summary(reps: list[str], out: Path) -> int:
    manifest: dict[str, Any] = {"_caveat": _CAVEAT, "categories": {}}
    print(_CAVEAT)
    print(f"\n{'category':28s} {'acted_wrong':>11} {'mismatch':>9} {'omit':>5} {'extra':>6} "
          f"{'path_div':>9} {'norm?':>6} {'unpars':>7}")
    print("-" * 92)
    rollup: Counter = Counter()
    subtypes: Counter = Counter()
    for rep in reps:
        rep_dir = (ROOT / rep) if not Path(rep).is_absolute() else Path(rep)
        category = rep_dir.name
        if not rep_dir.is_dir():
            print(f"{category:28s}  (rep dir not found: {rep_dir})")
            continue
        ans = _load_jsonl_by_id(BFCL_DATA / "possible_answer" / f"BFCL_v4_{category}.json")
        buckets: dict[str, list[str]] = defaultdict(list)
        detail: dict[str, Any] = {}
        cat_sub: Counter = Counter()
        n = 0
        for rec, res in _iter_failures(rep_dir, ans):
            n += 1
            pid = rec["id"]
            buckets[res["bucket"]].append(pid)
            rollup[res["bucket"]] += 1
            if res["bucket"] == "gt_value_mismatch":
                for m in res["mismatches"]:
                    cat_sub[m["subtype"]] += 1
                    subtypes[m["subtype"]] += 1
            detail[pid] = {"bucket": res["bucket"], "reason": rec.get("reason", ""),
                           "turn_shifted": res["turn_shifted"], "mismatches": res["mismatches"],
                           "omissions": res["omissions"], "extras": res["extras"],
                           "uncertain": res["uncertain"]}
        manifest["categories"][category] = {
            "n_acted_but_wrong": n,
            "buckets": {b: sorted(v, key=lambda p: int(p.rsplit('_', 1)[1])) for b, v in buckets.items()},
            "gt_value_mismatch_param_subtypes": dict(cat_sub),
            "detail": detail,
        }

        def c(b: str) -> int:
            return len(buckets.get(b, []))
        print(f"{category:28s} {n:>11} {c('gt_value_mismatch'):>9} {c('omission'):>5} "
              f"{c('extra_action'):>6} {c('path_divergence'):>9} {c('normalization_uncertain'):>6} "
              f"{c('unparsed'):>7}")

    manifest["rollup"] = dict(rollup)
    manifest["gt_value_mismatch_param_subtypes"] = dict(subtypes)
    total = sum(rollup.values())
    print("-" * 92)
    print(f"ROLLUP (A={total} acted-but-wrong): " +
          "  ".join(f"{b}={rollup[b]}" for b in
                    ("gt_value_mismatch", "omission", "extra_action", "path_divergence",
                     "normalization_uncertain", "unparsed")))
    if total:
        gvm = rollup["gt_value_mismatch"]
        print(f"gt_value_mismatch = {gvm}/{total} = {gvm/total:.1%} of A  (UPPER BOUND — skim to confirm)  "
              f"param subtypes: {dict(subtypes)}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2))
    print(f"\nmanifest → {out}")
    print("\nDecision gate (pre-registered): BANK unless confirmed gt_value_mismatch is BOTH >=20% of A "
          "AND >=~30 cases AND skim precision>=~0.6 AND a dominant separable cluster. See plan.")
    return 0


def _run_dump(reps: list[str], category: str | None, bucket: str | None, sample: int, seed: int) -> int:
    print(_CAVEAT)
    rng = random.Random(seed)
    for rep in reps:
        rep_dir = (ROOT / rep) if not Path(rep).is_absolute() else Path(rep)
        cat = rep_dir.name
        if category and category not in cat:
            continue
        if not rep_dir.is_dir():
            print(f"# (rep dir not found: {rep_dir})")
            continue
        ans = _load_jsonl_by_id(BFCL_DATA / "possible_answer" / f"BFCL_v4_{cat}.json")
        rows = [(rec, res) for rec, res in _iter_failures(rep_dir, ans)
                if bucket is None or res["bucket"] == bucket]
        rng.shuffle(rows)
        rows = rows[:sample]
        print(f"\n{'#'*84}\n# {cat}: {len(rows)} sampled (bucket={bucket or 'any'})\n{'#'*84}")
        for rec, res in sorted(rows, key=lambda r: int(r[0]['id'].rsplit('_', 1)[1])):
            pid = rec["id"]
            gt = (ans.get(pid) or {}).get("ground_truth", [])
            print(f"\n----- {pid} [{res['bucket']}{' turn_shifted' if res['turn_shifted'] else ''}] "
                  f"{rec.get('reason', '')}")
            print("  ASKS: " + " || ".join(a[:90] for a in _user_asks(rec)))
            print("  GT  per turn: " + " | ".join(f"t{i}:{t}" for i, t in enumerate(gt)))
            print("  MODEL per turn: " + " | ".join(
                f"t{i}:{[c for step in t for c in step]}" for i, t in enumerate(rec.get("decoded_turns", []))))
            for m in res["mismatches"]:
                print(f"    MISMATCH {m['fn']}.{m['param']} [{m['subtype']}]: model={m['model']!r} gt={m['gt']!r}")
            if res["omissions"]:
                print(f"    OMITTED fns: {res['omissions']}")
            if res["uncertain"]:
                print(f"    UNCERTAIN: {[(u['fn'], u['param']) for u in res['uncertain']]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Size the multi_turn acted-but-wrong corpus (read-only).")
    ap.add_argument("--rep", action="append", default=[],
                    help="Per-category record dir. Repeatable. Defaults to the two miss_* m5 reps.")
    ap.add_argument("--out", type=Path,
                    default=ROOT / "acceptance" / "bfcl" / "wrong_binding" / "sizing_manifest.json")
    ap.add_argument("--dump", action="store_true", help="Print a sampled skim instead of the summary.")
    ap.add_argument("--category", default=None, help="Restrict --dump to a category substring.")
    ap.add_argument("--bucket", default=None, help="Restrict --dump to one bucket (e.g. gt_value_mismatch).")
    ap.add_argument("--sample", type=int, default=20, help="--dump sample size per category.")
    ap.add_argument("--seed", type=int, default=20260525, help="--dump sampling seed (frozen).")
    args = ap.parse_args()
    reps = args.rep or _default_reps()
    if args.dump:
        return _run_dump(reps, args.category, args.bucket, args.sample, args.seed)
    return _run_summary(reps, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
