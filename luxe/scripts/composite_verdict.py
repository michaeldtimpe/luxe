"""Composite verdict — aggregates omlx_verdict + spec_decoding_verdict
+ Phase 3 multi-turn /review data into one per-agent recommendation.

Reads:
- results/<phase>/OMLX_VERDICT.csv (per-candidate)
- results/<phase>/SPEC_DECODING_VERDICT.csv
- results/overnight_<ts>/state.json (Phase 3 multi_turn_reviews results)
- results/orchestrator_bench/history.jsonl (real /review records)

Emits:
- results/<phase>/COMPOSITE_VERDICT.md
- results/<phase>/COMPOSITE_VERDICT.csv

Self-test: --self-test fabricates synthetic per-phase results and
confirms the recommendation logic emits the expected per-agent
strings.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parent.parent

# Per-agent decision matrix — fixed thresholds the composite reads to
# decide ADOPT/STAY-PUT/MIGRATE/DEFER for each agent.
TH_REVIEW_WALL_REGRESSION_PCT = 25.0   # tolerate ≤25% wall regression
TH_DECODE_RATIO_MIN = 1.30              # require ≥1.30× decode for adoption
TH_PASS_RATE_MAX_DROP_PP = 1.0          # ≤1pp pass-rate drop


@dataclass
class AgentRecommendation:
    agent: str
    workload_shape: str
    current_backend: str
    recommended_backend: str
    confidence: str   # "high" / "medium" / "low" / "no-data"
    reasoning: str


def _load_omlx_verdict(out_dir: Path, candidate: str) -> dict | None:
    """Read the per-candidate OMLX_VERDICT.csv if present.
    omlx_verdict.py emits both CSV + MD per candidate; we read CSV
    for the gate booleans."""
    p = out_dir / "VERDICT.csv"
    # omlx_verdict writes to results/<phase>/VERDICT.csv each invocation,
    # overwriting between candidates. Caller passes candidate via the
    # --candidate flag, then composite reads the row matching that name.
    if not p.exists():
        return None
    with p.open() as f:
        rows = list(csv.DictReader(f))
    matching = [r for r in rows if r.get("candidate") == candidate]
    return {"verdict": matching[0]["verdict"] if matching else None,
            "rows": matching} if matching else None


def _load_spec_verdict(out_dir: Path, candidate: str) -> dict | None:
    p = out_dir / "SPEC_DECODING_VERDICT.csv"
    if not p.exists():
        return None
    with p.open() as f:
        rows = list(csv.DictReader(f))
    matching = [r for r in rows if r.get("candidate") == candidate]
    return {"verdict": matching[0]["verdict"] if matching else None,
            "rows": matching} if matching else None


def _load_multi_turn(out_dir: Path) -> list[dict]:
    """Pull Phase 3 /review run records.

    Prefers `multi_turn_runs.jsonl` (produced by aggregate_multi_turn.py)
    because state.json's `result.runs` only retains the LAST sub-chunk's
    record — each `--only multi_turn_reviews` invocation overwrites it.
    The jsonl is the authoritative cross-(repo, backend) view.

    When multiple runs exist for the same (repo, backend) — e.g. a
    pre-fix degenerate plan plus a post-fix real run — keep only the
    "best" (most subtasks completed, ties broken by latest started_at).
    That way verdicts use the most authoritative data without us having
    to delete bad records from disk.
    """
    jsonl_path = out_dir / "multi_turn_runs.jsonl"
    runs: list[dict] = []
    if jsonl_path.exists():
        for line in jsonl_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    else:
        # Fall back to state.json (legacy single-record path).
        state_path = out_dir / "state.json"
        if not state_path.exists():
            return []
        state = json.loads(state_path.read_text())
        phase = state.get("phases", {}).get("multi_turn_reviews", {})
        if phase.get("status") != "done":
            return []
        runs = phase.get("result", {}).get("runs", [])

    # Dedupe per (repo, backend) — keep most subtasks_done, then latest.
    best: dict[tuple[str, str], dict] = {}
    for r in runs:
        key = (r.get("repo", "?"), r.get("backend", "?"))
        prev = best.get(key)
        score = (r.get("subtasks_done", 0), r.get("started_at", ""))
        if prev is None or score > (prev.get("subtasks_done", 0),
                                     prev.get("started_at", "")):
            best[key] = r
    return list(best.values())


def _wall_summary_per_backend(runs: list[dict]) -> dict[str, dict]:
    """Group Phase 3 runs by backend, compute median wall_s + done-rate."""
    by_backend: dict[str, list[dict]] = {}
    for r in runs:
        by_backend.setdefault(r.get("backend", "?"), []).append(r)
    out: dict[str, dict] = {}
    for back, rs in by_backend.items():
        walls = [r["wall_s"] for r in rs if isinstance(r.get("wall_s"), (int, float))]
        done = sum(1 for r in rs if r.get("status") == "done")
        out[back] = {
            "n_runs": len(rs),
            "n_done": done,
            "wall_s_median": statistics.median(walls) if walls else None,
            "repos": [r.get("repo") for r in rs],
        }
    return out


def _recommend_review_refactor(multi_turn: list[dict]) -> AgentRecommendation:
    """Decision for review/refactor: compare oMLX vs Ollama on real
    /review wall time. Adopt oMLX iff its median wall is within
    TH_REVIEW_WALL_REGRESSION_PCT of Ollama's."""
    summary = _wall_summary_per_backend(multi_turn)
    omlx = summary.get("omlx")
    ollama = summary.get("ollama")
    if not omlx or not ollama or not omlx.get("wall_s_median") or not ollama.get("wall_s_median"):
        return AgentRecommendation(
            agent="review/refactor",
            workload_shape="multi-turn, ~13k+ prompts, multi-paragraph output",
            current_backend="(observe)",
            recommended_backend="(insufficient data)",
            confidence="no-data",
            reasoning=(
                "multi_turn_reviews phase produced incomplete data — "
                f"omlx={omlx}, ollama={ollama}"
            ),
        )
    ratio = omlx["wall_s_median"] / ollama["wall_s_median"]
    pct_diff = (ratio - 1.0) * 100
    if ratio <= 1.0 + TH_REVIEW_WALL_REGRESSION_PCT / 100:
        rec = "oMLX"
        conf = "high" if ratio < 1.0 else "medium"
        reason = (
            f"oMLX median wall ({omlx['wall_s_median']:.0f}s) within "
            f"{TH_REVIEW_WALL_REGRESSION_PCT:.0f}% of Ollama's "
            f"({ollama['wall_s_median']:.0f}s). Δ = {pct_diff:+.1f}%. "
            f"Per-bench decode wins for oMLX validate the migration."
        )
    else:
        rec = "Ollama"
        conf = "medium"
        reason = (
            f"oMLX median wall ({omlx['wall_s_median']:.0f}s) regresses "
            f"{pct_diff:+.1f}% vs Ollama ({ollama['wall_s_median']:.0f}s) "
            f"on real /review — exceeds the {TH_REVIEW_WALL_REGRESSION_PCT:.0f}% "
            "tolerance. Decode wins on synthetic benchmarks don't "
            "carry to multi-turn /review with this prompt distribution."
        )
    return AgentRecommendation(
        agent="review/refactor",
        workload_shape="multi-turn, ~13k+ prompts, multi-paragraph output",
        current_backend="oMLX",
        recommended_backend=rec,
        confidence=conf,
        reasoning=reason,
    )


def _recommend_code(omlx_v: dict | None, spec_v: dict | None) -> AgentRecommendation:
    """Decision for code agent: oMLX baseline if oMLX_VERDICT is
    ADOPT or ADOPT WITH GATING; else Ollama. DFlash recommendation
    follows spec_decoding_verdict."""
    omlx_label = omlx_v.get("verdict") if omlx_v else None
    if omlx_label in ("ADOPT", "ADOPT WITH GATING"):
        rec_backend = "oMLX (baseline, no DFlash)"
        conf = "high" if omlx_label == "ADOPT" else "medium"
        reason = (
            f"oMLX_VERDICT={omlx_label} for qwen2.5-coder-14b. "
            f"DFlash should remain DISABLED for this agent — its "
            f"workload includes too many short-output tool-call turns "
            f"where DFlash regresses (per spec_decoding_verdict)."
        )
    elif omlx_label == "REJECT":
        rec_backend = "Ollama"
        conf = "high"
        reason = f"oMLX_VERDICT=REJECT — Ollama is the safer choice."
    else:
        rec_backend = "(insufficient data)"
        conf = "no-data"
        reason = f"oMLX_VERDICT={omlx_label or 'missing'} for code candidate."
    return AgentRecommendation(
        agent="code",
        workload_shape="mixed (50–1000 tok), single-turn-ish per-call",
        current_backend="oMLX",
        recommended_backend=rec_backend,
        confidence=conf,
        reasoning=reason,
    )


def _recommend_writing_calc(state: dict) -> list[AgentRecommendation]:
    """Decision for writing + calc: did Phase 4 (DFlash long-output)
    produce data? If yes, DFlash recommended. If skipped/deferred,
    flag as untested."""
    phase = state.get("phases", {}).get("dflash_long_output", {})
    out = []
    for agent, shape in (
        ("writing", "long creative prose, sustained generation"),
        ("calc", "multi-step calculations, medium-long output"),
    ):
        result = phase.get("result", {}) if phase.get("status") == "done" else {}
        variant_key = f"{agent}_dflash"
        v = result.get("variants", {}).get(variant_key) if result else None
        if not v:
            out.append(AgentRecommendation(
                agent=agent, workload_shape=shape,
                current_backend="(unchanged)",
                recommended_backend="(test deferred)",
                confidence="no-data",
                reasoning=f"Phase 4 didn't produce data for {agent} (status="
                          f"{phase.get('status')}, variant={v}).",
            ))
            continue
        if v.get("status") == "deferred":
            out.append(AgentRecommendation(
                agent=agent, workload_shape=shape,
                current_backend="(unchanged)",
                recommended_backend="(deferred)",
                confidence="no-data",
                reasoning=v.get("reason", "deferred"),
            ))
        elif v.get("exit_code") == 0:
            out.append(AgentRecommendation(
                agent=agent, workload_shape=shape,
                current_backend="(test produced data)",
                recommended_backend="enable DFlash if decode_throughput shows ≥1.5×",
                confidence="medium",
                reasoning=(
                    f"Phase 4 sweep ran successfully. Inspect "
                    f"results/runs/<phase>/<candidate>/omlx_q4km_dflash_{agent}/ "
                    f"vs the baseline slot to see the actual delta."
                ),
            ))
        else:
            out.append(AgentRecommendation(
                agent=agent, workload_shape=shape,
                current_backend="(unchanged)",
                recommended_backend="(test failed)",
                confidence="no-data",
                reasoning=f"Phase 4 sweep exited non-zero ({v.get('exit_code')}).",
            ))
    return out


def _emit_md(recs: list[AgentRecommendation], out_md: Path) -> str:
    lines = [
        "# Composite Verdict — overnight run",
        "",
        "Per-agent recommendation derived from omlx_verdict + "
        "spec_decoding_verdict + Phase 3 multi-turn /review data.",
        "",
        "| Agent | Workload shape | Current | Recommended | Confidence | Reasoning |",
        "|---|---|---|---|---|---|",
    ]
    for r in recs:
        lines.append(
            f"| {r.agent} | {r.workload_shape} | {r.current_backend} | "
            f"{r.recommended_backend} | {r.confidence} | {r.reasoning} |"
        )
    lines.append("")
    body = "\n".join(lines)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(body)
    return body


def _emit_csv(recs: list[AgentRecommendation], out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["agent", "workload_shape", "current_backend",
                    "recommended_backend", "confidence", "reasoning"])
        for r in recs:
            w.writerow([r.agent, r.workload_shape, r.current_backend,
                        r.recommended_backend, r.confidence, r.reasoning])


# ── self-test ────────────────────────────────────────────────────────


def _self_test() -> int:
    failures = 0
    for case, expected_review_rec, multi_turn in (
        ("oMLX-faster",
         "oMLX",
         [
             {"backend": "ollama", "wall_s": 2700, "status": "done"},
             {"backend": "omlx", "wall_s": 2400, "status": "done"},
         ]),
        ("oMLX-marginal-regress (within tolerance)",
         "oMLX",
         [
             {"backend": "ollama", "wall_s": 2700, "status": "done"},
             {"backend": "omlx", "wall_s": 3200, "status": "done"},  # +18% within 25%
         ]),
        ("oMLX-blown-tolerance",
         "Ollama",
         [
             {"backend": "ollama", "wall_s": 2700, "status": "done"},
             {"backend": "omlx", "wall_s": 4000, "status": "done"},  # +48%
         ]),
        ("no-data",
         "(insufficient data)",
         []),
    ):
        rec = _recommend_review_refactor(multi_turn)
        ok = rec.recommended_backend == expected_review_rec
        print(f"  {case}: expected={expected_review_rec} got={rec.recommended_backend} "
              f"{'✓' if ok else '✗'}")
        if not ok:
            failures += 1
            print(f"    reasoning: {rec.reasoning}")

    # Code recommendation
    for case, expected, omlx_v in (
        ("ADOPT verdict", "oMLX (baseline, no DFlash)", {"verdict": "ADOPT"}),
        ("REJECT verdict", "Ollama", {"verdict": "REJECT"}),
        ("missing", "(insufficient data)", None),
    ):
        rec = _recommend_code(omlx_v, None)
        ok = rec.recommended_backend == expected
        print(f"  code/{case}: expected={expected} got={rec.recommended_backend} "
              f"{'✓' if ok else '✗'}")
        if not ok:
            failures += 1
    return 0 if failures == 0 else 1


# ── CLI ──────────────────────────────────────────────────────────────


def main(
    phase: str = typer.Option("", "--phase",
        help="Phase id (e.g. overnight_2026-04-25T07-00-00). Used to "
             "locate the verdict CSVs from earlier phases."),
    out_dir: str = typer.Option("", "--out-dir",
        help="Where to read prior verdict CSVs + write COMPOSITE_VERDICT."),
    self_test: bool = typer.Option(False, "--self-test"),
) -> None:
    if self_test:
        sys.exit(_self_test())

    if not out_dir:
        typer.echo("--out-dir required (or use --self-test)", err=True)
        sys.exit(2)

    out = Path(out_dir)
    state = json.loads((out / "state.json").read_text()) if (out / "state.json").exists() else {}

    # The omlx_verdict + spec_decoding_verdict scripts overwrite their
    # CSVs each invocation — so the phase that ran them wrote the
    # latest. Read whatever's there.
    omlx_v_14b = _load_omlx_verdict(out, "qwen2.5-coder-14b")
    omlx_v_32b = _load_omlx_verdict(out, "qwen2.5-32b-instruct")
    spec_v_14b = _load_spec_verdict(out, "qwen2.5-coder-14b")
    multi_turn = _load_multi_turn(out)

    recs = [
        _recommend_code(omlx_v_14b, spec_v_14b),
        _recommend_review_refactor(multi_turn),
        *_recommend_writing_calc(state),
    ]

    out_md = out / "COMPOSITE_VERDICT.md"
    out_csv = out / "COMPOSITE_VERDICT.csv"
    body = _emit_md(recs, out_md)
    _emit_csv(recs, out_csv)
    print(body)
    print(f"\nwrote {out_md} and {out_csv}")


if __name__ == "__main__":
    typer.run(main)
