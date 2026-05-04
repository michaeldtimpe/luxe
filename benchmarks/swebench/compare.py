"""Paired pre/post SpecDD comparison — McNemar's test + W/L/T table.

PRELIMINARY (2026-05-03). Compares two checkpoints (pre vs post) on the
same instance subset; computes the appropriate paired statistic.

The user wants to know whether SpecDD Lever 2/3 shifts luxe's pass rate
on the n=75 SWE-bench Verified subset. Same instances pre and post →
paired McNemar's test on the discordant pairs is the right test;
unpaired t-test isn't applicable (binary outcomes).

Statistical power note: at n=75 with pre-rate 30-50%, McNemar at α=0.05
detects pass-rate deltas ≥10pp. Smaller deltas (3-5pp) are inconclusive
— surface explicitly in the verdict so the user doesn't claim a Lever 2
effect off a 5pp shift, or reject one off the same.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PairedCheckpoint:
    """One checkpoint's per-instance pass/fail outcomes."""
    name: str
    instance_passed: dict[str, bool] = field(default_factory=dict)


@dataclass
class PairedComparison:
    pre: PairedCheckpoint
    post: PairedCheckpoint
    contingency: dict[tuple[bool, bool], int] = field(default_factory=dict)
    pre_pass: int = 0
    post_pass: int = 0
    n: int = 0
    mcnemar_p: float = 1.0
    mcnemar_b: int = 0  # pre-pass → post-fail
    mcnemar_c: int = 0  # pre-fail → post-pass
    delta_pp: float = 0.0
    wilson_ci_low_pp: float = 0.0
    wilson_ci_high_pp: float = 0.0
    wins: list[str] = field(default_factory=list)
    losses: list[str] = field(default_factory=list)


def load_checkpoint_from_harness_summary(name: str, path: Path) -> PairedCheckpoint:
    """Load a PairedCheckpoint from a harness_summary.json (as written by
    `harness.py:write_harness_summary`).

    Expected shape: `{"instances": {iid: {"resolved": bool}, ...}, ...}`.
    """
    data = json.loads(Path(path).read_text())
    cp = PairedCheckpoint(name=name)
    for iid, info in data.get("instances", {}).items():
        cp.instance_passed[iid] = bool(info.get("resolved", False))
    return cp


def _mcnemar_continuity_corrected(b: int, c: int) -> float:
    """McNemar's chi-square with continuity correction → p-value
    (two-tailed). Falls back to exact binomial for small samples.
    """
    n = b + c
    if n == 0:
        return 1.0
    if n < 25:
        # Exact binomial: P(X <= min(b,c)) under H0 of fair coin, ×2
        from math import comb
        k = min(b, c)
        cum = sum(comb(n, i) for i in range(k + 1)) / (2 ** n)
        return min(1.0, 2 * cum)
    chi2 = ((abs(b - c) - 1) ** 2) / n
    # P(chi2 > x) for 1 df = erfc(sqrt(x/2)) — survival function
    return math.erfc(math.sqrt(chi2 / 2))


def _wilson_ci_pp(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% CI for a proportion, returning (low, high) in
    percentage-point units. n=0 returns (0, 0).
    """
    if n == 0:
        return 0.0, 0.0
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return 100 * (center - margin), 100 * (center + margin)


def compare(pre: PairedCheckpoint, post: PairedCheckpoint) -> PairedComparison:
    """Compute paired McNemar + W/L/T against shared instance set.

    Instances present only in one checkpoint are dropped from the paired
    analysis (with a warning surface upstream). The user expects same
    n=75 list at both checkpoints; mismatches indicate broken
    book-keeping, not an interesting biological signal.
    """
    shared = sorted(set(pre.instance_passed) & set(post.instance_passed))
    contingency = {(True, True): 0, (True, False): 0, (False, True): 0, (False, False): 0}
    wins: list[str] = []
    losses: list[str] = []
    for iid in shared:
        pre_p = pre.instance_passed[iid]
        post_p = post.instance_passed[iid]
        contingency[(pre_p, post_p)] += 1
        if not pre_p and post_p:
            wins.append(iid)
        elif pre_p and not post_p:
            losses.append(iid)

    n = len(shared)
    pre_pass = sum(1 for iid in shared if pre.instance_passed[iid])
    post_pass = sum(1 for iid in shared if post.instance_passed[iid])
    b = contingency[(True, False)]
    c = contingency[(False, True)]
    p_value = _mcnemar_continuity_corrected(b, c)
    delta_pp = (post_pass - pre_pass) / n * 100 if n else 0.0
    # Wilson CI on the pre rate; report the post-pre delta with a
    # rule-of-thumb power note rather than a paired-difference CI
    # (which requires McNemar-respecting bootstrap to be exact).
    pre_rate = pre_pass / n if n else 0.0
    low, high = _wilson_ci_pp(pre_rate, n)

    return PairedComparison(
        pre=pre,
        post=post,
        contingency=contingency,
        pre_pass=pre_pass,
        post_pass=post_pass,
        n=n,
        mcnemar_p=p_value,
        mcnemar_b=b,
        mcnemar_c=c,
        delta_pp=delta_pp,
        wilson_ci_low_pp=low,
        wilson_ci_high_pp=high,
        wins=wins,
        losses=losses,
    )


def render_text(cmp: PairedComparison) -> str:
    """Render a human-readable comparison report."""
    lines: list[str] = []
    lines.append(f"━━━ Paired comparison: {cmp.pre.name} vs {cmp.post.name}")
    lines.append(f"  n (shared instances): {cmp.n}")
    lines.append(f"  pre  pass rate: {cmp.pre_pass}/{cmp.n} = {100*cmp.pre_pass/max(cmp.n,1):.1f}%")
    lines.append(f"  post pass rate: {cmp.post_pass}/{cmp.n} = {100*cmp.post_pass/max(cmp.n,1):.1f}%")
    lines.append(f"  delta:          {cmp.delta_pp:+.1f}pp")
    lines.append(f"  pre Wilson 95%: [{cmp.wilson_ci_low_pp:.1f}pp, {cmp.wilson_ci_high_pp:.1f}pp]")
    lines.append("")
    lines.append("  Paired contingency (rows=pre, cols=post):")
    lines.append("                 post=PASS    post=FAIL")
    lines.append(f"    pre=PASS     {cmp.contingency[(True, True)]:>10}    {cmp.contingency[(True, False)]:>10}")
    lines.append(f"    pre=FAIL     {cmp.contingency[(False, True)]:>10}    {cmp.contingency[(False, False)]:>10}")
    lines.append("")
    lines.append(f"  McNemar (continuity-corrected): b={cmp.mcnemar_b} (pre-pass→post-fail),")
    lines.append(f"                                  c={cmp.mcnemar_c} (pre-fail→post-pass)")
    lines.append(f"  p-value: {cmp.mcnemar_p:.4f}")
    if cmp.mcnemar_p < 0.05:
        lines.append("  → significant at α=0.05")
    elif abs(cmp.delta_pp) >= 10:
        lines.append("  → ≥10pp shift but McNemar not significant — odd pattern, inspect contingency")
    else:
        lines.append("  → not significant; with n=75, deltas <10pp are below the noise floor")
    lines.append("")
    lines.append(f"  Wins ({len(cmp.wins)}, post-only): " + ", ".join(cmp.wins[:8]) +
                 (f" + {len(cmp.wins)-8} more" if len(cmp.wins) > 8 else ""))
    lines.append(f"  Losses ({len(cmp.losses)}, pre-only): " + ", ".join(cmp.losses[:8]) +
                 (f" + {len(cmp.losses)-8} more" if len(cmp.losses) > 8 else ""))
    return "\n".join(lines)
