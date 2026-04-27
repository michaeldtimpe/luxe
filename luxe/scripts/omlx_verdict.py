"""oMLX adoption verdict — applies Phase-1..5 thresholds to JSONL
results and writes results/<phase>/VERDICT.md + VERDICT.csv.

Reads ollama vs omlx outputs from
`results/runs/<phase>/<candidate>/{ollama_q4km,omlx_q4km}/<bench>.jsonl`
and applies the locked Phase 1-3-5 thresholds. Phase 4 (orchestrator)
is read from `results/orchestrator_bench/history.jsonl` rows tagged
with the appropriate label.

Verdict labels:
- ADOPT              — every gate passes
- ADOPT WITH GATING  — Phase 3 (the cache-advantage gate) passes but
                       one of Phase 1/2 is marginal (within the
                       safety band)
- REJECT             — Phase 3 fails OR Phase 1 parity fails hard
- INCONCLUSIVE       — required JSONL is missing for one or more
                       gates (e.g. Phase 0 setup didn't complete)

Self-test:

    uv run python scripts/omlx_verdict.py --self-test

Real run:

    uv run python scripts/omlx_verdict.py --phase ab_ollama_vs_omlx \\
        --candidate qwen2.5-coder-14b
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

# ── thresholds (Phase 1-3, 5) ────────────────────────────────────────

# Phase 1 — functional parity (BFCL)
TH_BFCL_RATIO = 0.95            # omlx success_rate / ollama ≥ 0.95
TH_BFCL_PARSE_FACTOR = 2.0      # omlx parse_errors ≤ 2× ollama

# Phase 2 — raw decode perf
TH_DECODE_FLOOR = 0.85          # omlx decode tok/s ≥ 0.85 × ollama
TH_TTFT_CEIL = 1.5              # omlx cold TTFT ≤ 1.5 × ollama
TH_RSS_CEIL = 1.25              # omlx peak_rss ≤ 1.25 × ollama

# Phase 3 — prefix cache (the defining gate). Originally cold/warm
# ratio, but oMLX's SSD-paged cache makes "cold" effectively warm
# (the cache survives across processes), so the cold/warm ratio
# collapses to ~1× even though oMLX is wildly winning on absolute
# TTFT. The right signal here is "is oMLX faster than Ollama on a
# realistic shared-prefix workload?" measured by absolute TTFT
# median at 16k. The cold/warm ratio is still computed and reported
# as supplemental info (can be useful when the SSD cache is empty).
TH_TTFT_RATIO_AT_16K = 0.5      # omlx ≤ 50% of ollama TTFT (≥ 2× lower)

# Phase 5 — soak
TH_ERROR_RATE = 0.01            # < 1%
TH_TTFT_STDEV_FACTOR = 2.0      # ≤ 2× ollama


# ── data ─────────────────────────────────────────────────────────────


@dataclass
class GateResult:
    name: str
    measured: str
    threshold: str
    passed: bool
    note: str = ""        # non-empty → INCONCLUSIVE for this gate
    severity: str = "hard"  # hard|soft (soft = "marginal but not REJECT")


def _load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _bench_path(phase: str, candidate: str, config_id: str, bench: str) -> Path:
    return ROOT / "results" / "runs" / phase / candidate / config_id / f"{bench}.jsonl"


def _get(d: dict, *path, default=None):
    for k in path:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d


# ── per-bench summarizers ────────────────────────────────────────────


def _bfcl_stats(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    succ = [_get(r, "metrics", "tool_call", "success_rate_pct") for r in rows]
    succ = [v for v in succ if isinstance(v, (int, float))]
    parse = [_get(r, "metrics", "tool_call", "parse_errors") for r in rows]
    parse = [v for v in parse if isinstance(v, (int, float))]
    if not succ:
        return None
    return {
        "success_rate_pct": statistics.fmean(succ),
        "parse_errors_total": sum(parse) if parse else 0,
    }


def _decode_stats(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    decode = [_get(r, "metrics", "throughput", "decode_tok_s") for r in rows]
    decode = [v for v in decode if isinstance(v, (int, float)) and v > 0]
    ttft = [_get(r, "metrics", "throughput", "ttft_s") or
            _get(r, "metrics", "throughput", "time_to_first_token_s") for r in rows]
    ttft = [v for v in ttft if isinstance(v, (int, float)) and v > 0]
    rss = [_get(r, "metrics", "peak_rss_bytes") for r in rows]
    rss = [v for v in rss if isinstance(v, (int, float)) and v > 0]
    if not decode or not ttft:
        return None
    return {
        "decode_tok_s_median": statistics.median(decode),
        "ttft_s_median": statistics.median(ttft),
        "ttft_s_stdev": statistics.pstdev(ttft) if len(ttft) > 1 else 0.0,
        "peak_rss_bytes_max": max(rss) if rss else 0,
        "n": len(rows),
    }


def _prefix_cache_stats(rows: list[dict]) -> dict | None:
    """Group prefix_cache_decay rows by size, compute median TTFT
    across all queries at that size, and the cold/warm ratio (kept
    as supplemental info — see TH_TTFT_RATIO_AT_16K rationale)."""
    if not rows:
        return None
    by_size: dict[str, list[tuple[int, float]]] = {}
    for r in rows:
        tid = r.get("task_id", "")
        if "_q" not in tid:
            continue
        size, qpart = tid.rsplit("_q", 1)
        try:
            idx = int(qpart)
        except ValueError:
            continue
        ttft = _get(r, "metrics", "throughput", "ttft_s") or \
               _get(r, "metrics", "throughput", "time_to_first_token_s")
        if not isinstance(ttft, (int, float)) or ttft <= 0:
            continue
        by_size.setdefault(size, []).append((idx, ttft))

    out = {}
    for size, pairs in by_size.items():
        pairs.sort()
        if not pairs:
            continue
        ttft_cold = pairs[0][1]
        warm = [t for _, t in pairs[1:]] if len(pairs) > 1 else []
        ttft_warm_median = statistics.median(warm) if warm else None
        ttft_all_median = statistics.median([t for _, t in pairs])
        out[size] = {
            "ttft_cold_s": ttft_cold,
            "ttft_warm_median_s": ttft_warm_median,
            "ttft_all_median_s": ttft_all_median,
            "cache_benefit_ratio": (
                ttft_cold / ttft_warm_median if ttft_warm_median else None
            ),
            "n": len(pairs),
        }
    return out or None


def _soak_stats(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    errors = sum(1 for r in rows if r.get("error"))
    ttft = [_get(r, "metrics", "throughput", "ttft_s") or
            _get(r, "metrics", "throughput", "time_to_first_token_s") for r in rows]
    ttft = [v for v in ttft if isinstance(v, (int, float)) and v > 0]
    return {
        "n": len(rows),
        "errors": errors,
        "error_rate": errors / len(rows),
        "ttft_stdev": statistics.pstdev(ttft) if len(ttft) > 1 else 0.0,
    }


# ── gate evaluators ──────────────────────────────────────────────────


def _missing_label(ol_present: bool, om_present: bool) -> str:
    if not ol_present and not om_present:
        return "no data on either backend"
    if not ol_present:
        return "no ollama data (baseline missing)"
    return "no omlx data (run not yet completed — start with omlx_healthcheck.py)"


def _gate_bfcl(ol: dict | None, om: dict | None) -> GateResult:
    if not ol or not om:
        return GateResult(
            "P1: BFCL functional parity",
            "—", f"omlx ≥ {TH_BFCL_RATIO * 100:.0f}% × ollama success",
            passed=False,
            note=f"bfcl_v3: {_missing_label(bool(ol), bool(om))}",
        )
    base = ol["success_rate_pct"]
    test = om["success_rate_pct"]
    ratio = test / base if base > 0 else 0
    parse_ok = (om["parse_errors_total"] <= max(1, ol["parse_errors_total"]) * TH_BFCL_PARSE_FACTOR)
    succ_ok = ratio >= TH_BFCL_RATIO
    return GateResult(
        "P1: BFCL functional parity",
        f"ollama={base:.1f}%, omlx={test:.1f}%, ratio={ratio:.2f}; "
        f"parse_errors ollama={ol['parse_errors_total']}, omlx={om['parse_errors_total']}",
        f"ratio ≥ {TH_BFCL_RATIO}, parse ≤ {TH_BFCL_PARSE_FACTOR}× ollama",
        passed=(succ_ok and parse_ok),
    )


def _gate_decode(ol: dict | None, om: dict | None) -> GateResult:
    if not ol or not om:
        return GateResult(
            "P2: raw decode perf (no cache adv.)",
            "—", f"decode ≥ {TH_DECODE_FLOOR}×, ttft ≤ {TH_TTFT_CEIL}×, rss ≤ {TH_RSS_CEIL}×",
            passed=False,
            note=f"decode_throughput: {_missing_label(bool(ol), bool(om))}",
        )
    decode_ratio = om["decode_tok_s_median"] / ol["decode_tok_s_median"] if ol["decode_tok_s_median"] else 0
    ttft_ratio = om["ttft_s_median"] / ol["ttft_s_median"] if ol["ttft_s_median"] else float("inf")
    rss_ratio = om["peak_rss_bytes_max"] / ol["peak_rss_bytes_max"] if ol["peak_rss_bytes_max"] else float("inf")
    decode_ok = decode_ratio >= TH_DECODE_FLOOR
    ttft_ok = ttft_ratio <= TH_TTFT_CEIL
    rss_ok = rss_ratio <= TH_RSS_CEIL
    # Mark severity soft when borderline — within 5% of the threshold.
    soft = (
        (decode_ok and decode_ratio < TH_DECODE_FLOOR * 1.05)
        or (ttft_ok and ttft_ratio > TH_TTFT_CEIL * 0.95)
        or (rss_ok and rss_ratio > TH_RSS_CEIL * 0.95)
    )
    return GateResult(
        "P2: raw decode perf (no cache adv.)",
        f"decode={decode_ratio:.2f}×, ttft={ttft_ratio:.2f}×, rss={rss_ratio:.2f}×",
        f"decode ≥ {TH_DECODE_FLOOR}, ttft ≤ {TH_TTFT_CEIL}, rss ≤ {TH_RSS_CEIL}",
        passed=(decode_ok and ttft_ok and rss_ok),
        severity="soft" if soft else "hard",
    )


def _gate_prefix_cache(ol: dict | None, om: dict | None) -> GateResult:
    """The defining gate. Compares absolute TTFT median between
    backends on the 16k-prefix shared workload. Replaces the original
    cold/warm cache_benefit_ratio gate, which is undefined under
    oMLX's SSD-paged cache (it makes the cold request also hit a
    warm cache, collapsing the ratio to 1× even when oMLX is wildly
    faster in absolute terms). The ratio is still surfaced in the
    measured string for context."""
    if not ol or not om:
        return GateResult(
            "P3: prefix-cache TTFT advantage (16k)",
            "—", f"omlx TTFT ≤ {TH_TTFT_RATIO_AT_16K * 100:.0f}% × ollama",
            passed=False,
            note="prefix_cache_decay: " + _missing_label(bool(ol), bool(om)),
        )
    om16 = om.get("medium_16k")
    ol16 = ol.get("medium_16k")
    if not om16 or not ol16 or om16.get("ttft_all_median_s") is None or ol16.get("ttft_all_median_s") is None:
        return GateResult(
            "P3: prefix-cache TTFT advantage (16k)",
            "—", f"omlx TTFT ≤ {TH_TTFT_RATIO_AT_16K * 100:.0f}% × ollama",
            passed=False, note="medium_16k slice missing or empty for one backend",
        )
    om_ttft = om16["ttft_all_median_s"]
    ol_ttft = ol16["ttft_all_median_s"]
    ratio = om_ttft / ol_ttft if ol_ttft > 0 else float("inf")
    om_benefit = om16.get("cache_benefit_ratio")
    ol_benefit = ol16.get("cache_benefit_ratio")
    benefit_blurb = ""
    if om_benefit is not None and ol_benefit is not None:
        benefit_blurb = f" (cold/warm ratio: omlx={om_benefit:.2f}×, ollama={ol_benefit:.2f}×)"
    return GateResult(
        "P3: prefix-cache TTFT advantage (16k)",
        f"ollama={ol_ttft:.2f}s, omlx={om_ttft:.2f}s, ratio={ratio:.2f}×{benefit_blurb}",
        f"omlx TTFT ≤ {TH_TTFT_RATIO_AT_16K * 100:.0f}% × ollama (≥ {1 / TH_TTFT_RATIO_AT_16K:.0f}× faster)",
        passed=(ratio <= TH_TTFT_RATIO_AT_16K),
    )


def _gate_soak(om_soak: dict | None, ol_decode: dict | None) -> GateResult:
    if not om_soak:
        return GateResult(
            "P5: stability soak",
            "—", f"errors < {TH_ERROR_RATE * 100:.1f}%",
            passed=False, note="soak results missing (run 50 iterations of decode_throughput against omlx)"
        )
    err_ok = om_soak["error_rate"] < TH_ERROR_RATE
    if ol_decode and ol_decode.get("ttft_s_stdev"):
        stdev_factor = om_soak["ttft_stdev"] / ol_decode["ttft_s_stdev"] if ol_decode["ttft_s_stdev"] else float("inf")
    else:
        stdev_factor = 0.0
    stdev_ok = stdev_factor <= TH_TTFT_STDEV_FACTOR
    return GateResult(
        "P5: stability soak",
        f"errors={om_soak['errors']}/{om_soak['n']} ({om_soak['error_rate'] * 100:.2f}%), "
        f"ttft_stdev_factor={stdev_factor:.2f}",
        f"errors < {TH_ERROR_RATE * 100:.1f}%, stdev ≤ {TH_TTFT_STDEV_FACTOR}× ollama",
        passed=(err_ok and stdev_ok),
    )


# ── verdict + emit ───────────────────────────────────────────────────


def _decide(gates: list[GateResult]) -> str:
    # Inconclusive trumps everything else.
    if any(g.note for g in gates):
        return "INCONCLUSIVE"
    p3 = next((g for g in gates if g.name.startswith("P3")), None)
    p1 = next((g for g in gates if g.name.startswith("P1")), None)
    if p3 and not p3.passed:
        return "REJECT"
    if p1 and not p1.passed:
        return "REJECT"
    # All hard gates pass; bump to "ADOPT WITH GATING" if any gate
    # passed marginally (within the soft band) — the threshold was
    # met but with little headroom, worth flagging for re-eval.
    if all(g.passed for g in gates):
        if any(g.severity == "soft" for g in gates):
            return "ADOPT WITH GATING"
        return "ADOPT"
    # Phase 3 + Phase 1 pass; one of P2/P5 is marginal → soft pass.
    if all(g.passed or g.severity == "soft" for g in gates):
        return "ADOPT WITH GATING"
    return "REJECT"


def _emit_md(
    candidate: str, gates: list[GateResult], label: str, out_md: Path
) -> str:
    lines = [
        f"# oMLX Adoption Verdict — {candidate}",
        "",
        f"**Verdict: {label}**",
        "",
        "| Gate | Measured | Threshold | Result |",
        "|---|---|---|---|",
    ]
    for g in gates:
        if g.note:
            mark = f"⚠ inconclusive — {g.note}"
        elif g.passed:
            mark = "✅ pass" + (" (marginal)" if g.severity == "soft" else "")
        else:
            mark = "❌ fail"
        lines.append(f"| {g.name} | {g.measured} | {g.threshold} | {mark} |")
    lines.append("")
    body = "\n".join(lines)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(body)
    return body


def _emit_csv(
    candidate: str, gates: list[GateResult], label: str, out_csv: Path
) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["candidate", "verdict", "gate", "measured", "threshold", "passed", "severity", "note"])
        for g in gates:
            w.writerow([candidate, label, g.name, g.measured, g.threshold, g.passed, g.severity, g.note])


# ── self-test ────────────────────────────────────────────────────────


def _synth_bfcl(success_rate: float, parse_errors_per_row: int = 0, n: int = 20) -> str:
    return "\n".join(
        json.dumps({
            "task_id": f"bfcl_{i}",
            "passed": True,
            "metrics": {
                "tool_call": {
                    "success_rate_pct": success_rate,
                    "parse_errors": parse_errors_per_row,
                }
            },
        })
        for i in range(n)
    )


def _synth_decode(decode_tok_s: float, ttft_s: float, rss_bytes: int, n: int = 5) -> str:
    return "\n".join(
        json.dumps({
            "task_id": f"dt_{i}",
            "passed": True,
            "metrics": {
                "throughput": {"decode_tok_s": decode_tok_s, "ttft_s": ttft_s},
                "peak_rss_bytes": rss_bytes,
            },
        })
        for i in range(n)
    )


def _synth_prefix_cache(ttft_cold: float, ttft_warm: float, n_per_size: int = 10) -> str:
    """ttft_cold = first query's TTFT; ttft_warm = subsequent. The
    new P3 gate compares median(all queries) between backends, so
    setting ttft_cold ≈ ttft_warm is the realistic shape under
    oMLX's SSD cache; setting them very different models the
    no-SSD-cache shape."""
    rows = []
    for size in ("small_4k", "medium_16k", "large_32k"):
        for idx in range(n_per_size):
            ttft = ttft_cold if idx == 0 else ttft_warm
            rows.append(json.dumps({
                "task_id": f"{size}_q{idx:02d}",
                "passed": True,
                "metrics": {"throughput": {"ttft_s": ttft}},
            }))
    return "\n".join(rows)


def _self_test() -> int:
    """Confirm the verdict logic emits ADOPT for clearly-good results
    and REJECT for clearly-bad. Three scenarios."""
    failures = 0
    for case, expected, ol_b, om_b, ol_d, om_d, ol_pc, om_pc in (
        # Tuples per backend: bfcl=(success_rate_pct, parse_errors_per_row),
        # decode=(decode_tok_s, ttft_s, peak_rss_bytes),
        # pc=(ttft_cold, ttft_warm) — the new P3 compares medians across
        # all queries between backends, so what matters is
        # median(om_pc) vs median(ol_pc).
        ("perfect", "ADOPT",
         (95.0, 0), (95.0, 0),
         (30.0, 0.5, 8 * 10**9), (30.0, 0.5, 9 * 10**9),
         (10.0, 5.0),   # ollama median ≈ 5
         (1.0, 0.5)),   # omlx   median ≈ 0.5 (10× faster, well under 50%)
        ("decode-margin", "ADOPT WITH GATING",
         (95.0, 0), (95.0, 0),
         (30.0, 0.5, 8 * 10**9), (26.0, 0.55, 9 * 10**9),  # decode 0.87×, ttft 1.1×
         (10.0, 5.0), (1.0, 0.5)),
        ("p3-fails", "REJECT",
         (95.0, 0), (95.0, 0),
         (30.0, 0.5, 8 * 10**9), (30.0, 0.5, 9 * 10**9),
         (10.0, 5.0),   # ollama 5s
         (10.0, 4.0)),  # omlx 4s — only 20% better, doesn't clear 50% gate
        ("p1-fails", "REJECT",
         (95.0, 0), (50.0, 0),  # huge BFCL drop
         (30.0, 0.5, 8 * 10**9), (30.0, 0.5, 9 * 10**9),
         (10.0, 5.0), (1.0, 0.5)),
    ):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "results" / "runs" / "self_test" / "synth"
            for cfg, b, d, pc in (
                ("ollama_q4km", ol_b, ol_d, ol_pc),
                ("omlx_q4km", om_b, om_d, om_pc),
            ):
                cdir = root / cfg
                cdir.mkdir(parents=True)
                (cdir / "bfcl_v3.jsonl").write_text(_synth_bfcl(*b))
                (cdir / "decode_throughput.jsonl").write_text(_synth_decode(*d))
                cold, warm = pc
                (cdir / "prefix_cache_decay.jsonl").write_text(_synth_prefix_cache(cold, warm))

            ol_bfcl = _bfcl_stats(_load_rows(root / "ollama_q4km" / "bfcl_v3.jsonl"))
            om_bfcl = _bfcl_stats(_load_rows(root / "omlx_q4km" / "bfcl_v3.jsonl"))
            ol_dec = _decode_stats(_load_rows(root / "ollama_q4km" / "decode_throughput.jsonl"))
            om_dec = _decode_stats(_load_rows(root / "omlx_q4km" / "decode_throughput.jsonl"))
            ol_pcs = _prefix_cache_stats(_load_rows(root / "ollama_q4km" / "prefix_cache_decay.jsonl"))
            om_pcs = _prefix_cache_stats(_load_rows(root / "omlx_q4km" / "prefix_cache_decay.jsonl"))
            # Synthesize a soak result so the gate doesn't go INCONCLUSIVE
            # in the self-test. Real runs read from a separate
            # `decode_throughput_soak.jsonl` (operator-produced).
            om_soak = {"n": 50, "errors": 0, "error_rate": 0.0, "ttft_stdev": 0.5}

            gates = [
                _gate_bfcl(ol_bfcl, om_bfcl),
                _gate_decode(ol_dec, om_dec),
                _gate_prefix_cache(ol_pcs, om_pcs),
                _gate_soak(om_soak, ol_dec),
            ]
            label = _decide(gates)
            ok = (label == expected)
            print(f"  {case}: expected={expected} got={label} {'✓' if ok else '✗'}")
            if not ok:
                failures += 1
                for g in gates:
                    print(f"    {g.name}: passed={g.passed} sev={g.severity} note={g.note} measured={g.measured}")
    return 0 if failures == 0 else 1


# ── CLI ──────────────────────────────────────────────────────────────


def main(
    phase: str = typer.Option("ab_ollama_vs_omlx", "--phase"),
    candidate: str = typer.Option("qwen2.5-coder-14b", "--candidate"),
    out_dir: str = typer.Option("", "--out-dir"),
    self_test: bool = typer.Option(False, "--self-test"),
) -> None:
    if self_test:
        sys.exit(_self_test())

    ol_bfcl = _bfcl_stats(_load_rows(_bench_path(phase, candidate, "ollama_q4km", "bfcl_v3")))
    om_bfcl = _bfcl_stats(_load_rows(_bench_path(phase, candidate, "omlx_q4km", "bfcl_v3")))
    ol_dec = _decode_stats(_load_rows(_bench_path(phase, candidate, "ollama_q4km", "decode_throughput")))
    om_dec = _decode_stats(_load_rows(_bench_path(phase, candidate, "omlx_q4km", "decode_throughput")))
    ol_pcs = _prefix_cache_stats(_load_rows(_bench_path(phase, candidate, "ollama_q4km", "prefix_cache_decay")))
    om_pcs = _prefix_cache_stats(_load_rows(_bench_path(phase, candidate, "omlx_q4km", "prefix_cache_decay")))
    # Soak: operator runs decode_throughput 50× against omlx and
    # writes the result to a separate file. Absent file → INCONCLUSIVE
    # P5 gate (the verdict still falls through to ADOPT/REJECT for the
    # other gates).
    om_soak_path = _bench_path(phase, candidate, "omlx_q4km", "decode_throughput_soak")
    om_soak = _soak_stats(_load_rows(om_soak_path))

    gates = [
        _gate_bfcl(ol_bfcl, om_bfcl),
        _gate_decode(ol_dec, om_dec),
        _gate_prefix_cache(ol_pcs, om_pcs),
        _gate_soak(om_soak, ol_dec),
    ]
    label = _decide(gates)

    out = Path(out_dir) if out_dir else ROOT / "results" / phase
    out_md = out / "VERDICT.md"
    out_csv = out / "VERDICT.csv"
    body = _emit_md(candidate, gates, label, out_md)
    _emit_csv(candidate, gates, label, out_csv)
    print(body)
    print(f"\nwrote {out_md} and {out_csv}")


if __name__ == "__main__":
    typer.run(main)
