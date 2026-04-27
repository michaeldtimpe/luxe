"""Speculative-decoding verdict — apply Phase A decision gates to
existing JSONL results and emit SPEC_DECODING_VERDICT.md (+ .csv).

Reads `results/runs/<phase>/<candidate>/{baseline,spec-only}/<bench>.jsonl`
and applies three gates:

  Gate 1 — decode tok/s ≥ 1.5 × baseline (worth pursuing)
  Gate 2 — BFCL tool-call success rate within 1 pp of baseline (safe)
  Gate 3 — HumanEval+ pass rate within 1 pp of baseline (preserved)

Writes the verdict to `results/<phase>/SPEC_DECODING_VERDICT.md` with
ADOPT / REJECT / INCONCLUSIVE. Pure stdlib; no model invocation.

Example:

    uv run python scripts/spec_decoding_verdict.py \\
        --phase phase_a \\
        --candidate qwen2.5-coder-14b

Self-test (synthesizes perfect + terrible result rows, confirms the
verdict logic emits ADOPT vs REJECT):

    uv run python scripts/spec_decoding_verdict.py --self-test
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

# Decision gates (locked for the purpose of this verdict — encode them
# here rather than in optimization_configs.yaml so the verdict is
# self-contained and reproducible).
GATE_DECODE_RATIO = 1.5     # spec / baseline decode tok/s
GATE_BFCL_DELTA_PP = 1.0    # absolute pp drop allowed
GATE_HEP_DELTA_PP = 1.0     # absolute pp drop allowed


# ── data structures ──────────────────────────────────────────────────


@dataclass
class BenchSummary:
    bench: str
    n: int
    pass_rate_pct: float | None      # passed.mean * 100 (HumanEval+, MBPP+)
    decode_tok_s_median: float | None
    bfcl_success_rate_pct: float | None  # mean of metrics.tool_call.success_rate_pct (BFCL)


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


def _summarize(rows: list[dict], bench: str) -> BenchSummary:
    n = len(rows)
    if not n:
        return BenchSummary(bench, 0, None, None, None)
    passed = [bool(r.get("passed")) for r in rows]
    pass_rate = sum(passed) / len(passed) * 100 if passed else None
    decode = [
        (r.get("metrics") or {}).get("throughput", {}).get("decode_tok_s")
        for r in rows
    ]
    decode = [d for d in decode if isinstance(d, (int, float)) and d > 0]
    decode_med = statistics.median(decode) if decode else None
    bfcl = [
        (r.get("metrics") or {}).get("tool_call", {}).get("success_rate_pct")
        for r in rows
    ]
    bfcl = [v for v in bfcl if isinstance(v, (int, float))]
    # BFCL only — other benches don't fire tool calls so this stays None.
    bfcl_rate = (sum(bfcl) / len(bfcl)) if bfcl and bench == "bfcl_v3" else None
    return BenchSummary(bench, n, pass_rate, decode_med, bfcl_rate)


def _config_summaries(
    phase: str, candidate: str, config_id: str, benches: list[str]
) -> dict[str, BenchSummary]:
    base = ROOT / "results" / "runs" / phase / candidate / config_id
    return {b: _summarize(_load_rows(base / f"{b}.jsonl"), b) for b in benches}


# ── verdict logic ────────────────────────────────────────────────────


@dataclass
class GateResult:
    name: str
    measured: str        # human-readable measured values
    threshold: str       # human-readable threshold
    passed: bool
    note: str = ""


def _gate_decode(
    base: dict[str, BenchSummary], spec: dict[str, BenchSummary]
) -> GateResult:
    """Decode tok/s gate uses the decode_throughput benchmark
    specifically (its job is pure perf signal). If the bench isn't
    present in either config, the gate is INCONCLUSIVE rather than
    silently passing."""
    b = base.get("decode_throughput")
    s = spec.get("decode_throughput")
    if not b or not s or b.decode_tok_s_median is None or s.decode_tok_s_median is None:
        return GateResult(
            "G1: decode tok/s ≥ 1.5× baseline",
            "—", f"≥ {GATE_DECODE_RATIO}×",
            passed=False,
            note="decode_throughput results missing for one or both configs",
        )
    ratio = s.decode_tok_s_median / b.decode_tok_s_median
    return GateResult(
        "G1: decode tok/s ≥ 1.5× baseline",
        f"baseline={b.decode_tok_s_median:.1f} tok/s, spec={s.decode_tok_s_median:.1f} tok/s, ratio={ratio:.2f}×",
        f"≥ {GATE_DECODE_RATIO}×",
        passed=ratio >= GATE_DECODE_RATIO,
    )


def _gate_bfcl(
    base: dict[str, BenchSummary], spec: dict[str, BenchSummary]
) -> GateResult:
    b = base.get("bfcl_v3")
    s = spec.get("bfcl_v3")
    if not b or not s or b.bfcl_success_rate_pct is None or s.bfcl_success_rate_pct is None:
        return GateResult(
            "G2: BFCL tool-call success ≤ 1 pp drop",
            "—", f"≤ {GATE_BFCL_DELTA_PP} pp drop",
            passed=False,
            note="bfcl_v3 results missing for one or both configs",
        )
    delta = s.bfcl_success_rate_pct - b.bfcl_success_rate_pct
    return GateResult(
        "G2: BFCL tool-call success ≤ 1 pp drop",
        f"baseline={b.bfcl_success_rate_pct:.1f}%, spec={s.bfcl_success_rate_pct:.1f}%, Δ={delta:+.1f} pp",
        f"≤ {GATE_BFCL_DELTA_PP} pp drop",
        passed=delta >= -GATE_BFCL_DELTA_PP,
    )


def _gate_hep(
    base: dict[str, BenchSummary], spec: dict[str, BenchSummary]
) -> GateResult:
    b = base.get("humaneval_plus")
    s = spec.get("humaneval_plus")
    if not b or not s or b.pass_rate_pct is None or s.pass_rate_pct is None:
        return GateResult(
            "G3: HumanEval+ pass rate ≤ 1 pp drop",
            "—", f"≤ {GATE_HEP_DELTA_PP} pp drop",
            passed=False,
            note="humaneval_plus results missing for one or both configs",
        )
    delta = s.pass_rate_pct - b.pass_rate_pct
    return GateResult(
        "G3: HumanEval+ pass rate ≤ 1 pp drop",
        f"baseline={b.pass_rate_pct:.1f}%, spec={s.pass_rate_pct:.1f}%, Δ={delta:+.1f} pp",
        f"≤ {GATE_HEP_DELTA_PP} pp drop",
        passed=delta >= -GATE_HEP_DELTA_PP,
    )


def _verdict_label(gates: list[GateResult]) -> str:
    inconclusive = any(g.note for g in gates)
    if inconclusive:
        return "INCONCLUSIVE"
    if all(g.passed for g in gates):
        return "ADOPT"
    return "REJECT"


def _emit_md(
    candidate: str,
    base_cfg: str,
    spec_cfg: str,
    gates: list[GateResult],
    label: str,
    out_md: Path,
) -> str:
    lines = [
        f"# Speculative Decoding Verdict — {candidate}",
        "",
        f"**Verdict: {label}**",
        "",
        f"- baseline config: `{base_cfg}`",
        f"- spec config: `{spec_cfg}`",
        "",
        "| Gate | Measured | Threshold | Result |",
        "|---|---|---|---|",
    ]
    for g in gates:
        mark = "✅ pass" if g.passed else ("⚠ inconclusive" if g.note else "❌ fail")
        if g.note:
            mark += f" — {g.note}"
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
        w.writerow(["candidate", "verdict", "gate", "measured", "threshold", "passed", "note"])
        for g in gates:
            w.writerow([candidate, label, g.name, g.measured, g.threshold, g.passed, g.note])


# ── self-test ────────────────────────────────────────────────────────


def _synth(decode_tok_s: float, passed: bool, bfcl_rate: float) -> dict:
    """Synthesize a single JSONL row with the relevant fields populated.
    Used by --self-test to confirm verdict logic without running an
    actual sweep. Caller controls whether this individual row passes —
    aggregate the booleans across rows to hit a target pass rate."""
    return {
        "task_id": "synth",
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "metrics": {
            "throughput": {"decode_tok_s": decode_tok_s},
            "tool_call": {"success_rate_pct": bfcl_rate},
        },
    }


def _synth_rows(n: int, decode_tok_s: float, pass_rate_pct: float, bfcl_rate: float) -> str:
    """Build n JSONL rows with aggregate pass-rate matching the target."""
    n_pass = round(n * pass_rate_pct / 100)
    rows = []
    for i in range(n):
        rows.append(json.dumps(_synth(decode_tok_s, i < n_pass, bfcl_rate)))
    return "\n".join(rows)


def _self_test() -> int:
    """Synthesize 'perfect' (clearly ADOPT) and 'terrible' (clearly
    REJECT) result trees, confirm the verdict matches expectation."""
    failures = 0
    for case, expected, base_dec, spec_dec, base_bfcl, spec_bfcl, base_hep, spec_hep in (
        ("perfect", "ADOPT", 30.0, 60.0, 95.0, 95.0, 80.0, 80.0),
        ("terrible-decode", "REJECT", 30.0, 32.0, 95.0, 95.0, 80.0, 80.0),
        ("terrible-bfcl", "REJECT", 30.0, 60.0, 95.0, 80.0, 80.0, 80.0),
        ("terrible-hep", "REJECT", 30.0, 60.0, 95.0, 95.0, 80.0, 60.0),
    ):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "results" / "runs" / "self_test" / "synth"
            for cfg, dec, bfcl, hep in (
                ("baseline", base_dec, base_bfcl, base_hep),
                ("spec-only", spec_dec, spec_bfcl, spec_hep),
            ):
                d = root / cfg
                d.mkdir(parents=True)
                (d / "decode_throughput.jsonl").write_text(
                    _synth_rows(5, dec, 100.0, 0.0)
                )
                (d / "humaneval_plus.jsonl").write_text(
                    _synth_rows(20, 0, hep, 0.0)
                )
                (d / "bfcl_v3.jsonl").write_text(
                    _synth_rows(20, 0, 100.0, bfcl)
                )
            # Override module-level path resolution for the test by
            # building the summaries directly.
            benches = ["decode_throughput", "humaneval_plus", "bfcl_v3"]
            base = {b: _summarize(_load_rows(root / "baseline" / f"{b}.jsonl"), b) for b in benches}
            spec = {b: _summarize(_load_rows(root / "spec-only" / f"{b}.jsonl"), b) for b in benches}
            gates = [_gate_decode(base, spec), _gate_bfcl(base, spec), _gate_hep(base, spec)]
            label = _verdict_label(gates)
            ok = (label == expected)
            print(f"  {case}: expected={expected} got={label} {'✓' if ok else '✗'}")
            if not ok:
                failures += 1
                for g in gates:
                    print(f"    {g.name}: passed={g.passed} measured={g.measured}")
    return 0 if failures == 0 else 1


# ── CLI ──────────────────────────────────────────────────────────────


def main(
    phase: str = typer.Option("phase_a", "--phase"),
    candidate: str = typer.Option("qwen2.5-coder-14b", "--candidate"),
    baseline_cfg: str = typer.Option("baseline", "--baseline-cfg"),
    spec_cfg: str = typer.Option("spec-only", "--spec-cfg"),
    out_dir: str = typer.Option("", "--out-dir"),
    self_test: bool = typer.Option(False, "--self-test"),
) -> None:
    if self_test:
        sys.exit(_self_test())

    benches = ["decode_throughput", "humaneval_plus", "bfcl_v3"]
    base = _config_summaries(phase, candidate, baseline_cfg, benches)
    spec = _config_summaries(phase, candidate, spec_cfg, benches)

    gates = [_gate_decode(base, spec), _gate_bfcl(base, spec), _gate_hep(base, spec)]
    label = _verdict_label(gates)

    out = Path(out_dir) if out_dir else ROOT / "results" / phase
    out_md = out / "SPEC_DECODING_VERDICT.md"
    out_csv = out / "SPEC_DECODING_VERDICT.csv"
    body = _emit_md(candidate, baseline_cfg, spec_cfg, gates, label, out_md)
    _emit_csv(candidate, gates, label, out_csv)
    print(body)
    print(f"\nwrote {out_md} and {out_csv}")


if __name__ == "__main__":
    typer.run(main)
