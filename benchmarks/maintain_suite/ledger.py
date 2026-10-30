"""Run-ledger audit: does a bench output dir account for exactly what it claims?

Harvested from harness2's `Ledger.assert_identity` (acceptance/harvest_2026_10).
A bench result is trustworthy only if every credited cell was produced by its
own run and graded at its own commit. The failures this catches have all
happened here: a reused branch name graded another run's commit
(2026-10-22, fixed by recording head_sha_used), and a hand-verify
misattributed diffs to the wrong run (maintain_suite.sdd).

Checks, duplicates FIRST because a duplicate can satisfy the counts:

- duplicate_run_id  one luxe_run_id credited in two state files
- shared_head_sha   one graded commit credited to two state files
                    (a run that changed nothing, head == base, is exempt)
- missing_diff      DONE at a recorded head_sha_used with no diff.patch
- missing_cell      an expected (variant, fixture) cell with no state.json

States recorded before head_sha_used existed (2026-10-22) cannot be checked
for the commit rules; they are counted as legacy, never reported as problems.

Read-only. Reports; never raises on a malformed file (that is itself reported).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

PROBLEM_KINDS = ("duplicate_run_id", "shared_head_sha", "missing_diff",
                 "missing_cell", "unreadable_state")


@dataclass(frozen=True)
class Problem:
    kind: str
    detail: str
    paths: tuple[str, ...] = ()


@dataclass
class LedgerReport:
    root: str
    states: int = 0
    legacy: int = 0          # DONE states with no head_sha_used (pre-2026-10-22)
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {
            "root": self.root, "states": self.states, "legacy": self.legacy,
            "ok": self.ok,
            "problems": [{"kind": p.kind, "detail": p.detail, "paths": list(p.paths)}
                         for p in self.problems],
        }


def _state_files(root: Path, expected: list[tuple[str, str]] | None = None) -> list[Path]:
    """The plan's own cells when `expected` is given (the default --output is
    ./acceptance, which holds every earlier bench); otherwise every state.json
    under root, skipping `_`-prefixed dirs (overlays)."""
    if expected is not None:
        cells = [root / vid / fid / "state.json" if vid else root / fid / "state.json"
                 for vid, fid in expected]
        return [p for p in cells if p.is_file()]
    out = []
    for p in sorted(root.rglob("state.json")):
        rel = p.relative_to(root).parts
        if any(part.startswith("_") for part in rel[:-1]):
            continue
        out.append(p)
    return out


def check_ledger(
    root: Path | str,
    expected: list[tuple[str, str]] | None = None,
) -> LedgerReport:
    """Audit one bench output dir. `expected` is the (variant_id, fixture_id)
    plan; variant_id "" means the legacy single-variant layout."""
    root = Path(root)
    report = LedgerReport(root=str(root))
    by_run: dict[str, list[str]] = defaultdict(list)
    by_head: dict[str, list[str]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()

    for sf in _state_files(root, expected):
        report.states += 1
        rel = str(sf.relative_to(root))
        try:
            st = json.loads(sf.read_text())
        except (OSError, json.JSONDecodeError) as e:
            report.problems.append(Problem("unreadable_state", f"{rel}: {e}", (rel,)))
            continue
        fixture_id = str(st.get("fixture_id", ""))
        variant_id = sf.parent.parent.name if sf.parent.parent != root else ""
        seen.add((variant_id, fixture_id))

        run_id = str(st.get("luxe_run_id", ""))
        if run_id:
            by_run[run_id].append(rel)

        if st.get("status") != "done":
            continue
        head = str(st.get("head_sha_used", ""))
        if not head:
            report.legacy += 1
            continue
        if head != str(st.get("base_sha_used", "")):
            by_head[head].append(rel)
        if not (sf.parent / "diff.patch").is_file():
            report.problems.append(Problem(
                "missing_diff", f"{rel} is DONE at {head[:12]} but has no diff.patch",
                (rel,)))

    for run_id, rels in sorted(by_run.items()):
        if len(rels) > 1:
            report.problems.insert(0, Problem(
                "duplicate_run_id",
                f"run {run_id} is credited in {len(rels)} cells; at most one of them "
                f"was actually produced by it", tuple(rels)))
    for head, rels in sorted(by_head.items()):
        if len(rels) > 1:
            report.problems.insert(0, Problem(
                "shared_head_sha",
                f"commit {head[:12]} is graded for {len(rels)} cells; each run makes "
                f"its own commit, so all but one are misattributed", tuple(rels)))
    for cell in expected or []:
        if cell not in seen:
            vid, fid = cell
            report.problems.append(Problem(
                "missing_cell", f"no state.json for {vid + '/' if vid else ''}{fid}"))
    return report


def render(report: LedgerReport) -> str:
    """The console block run.main prints after the Summary."""
    head = (f"  ledger     : {report.states} state(s), {report.legacy} legacy "
            f"(no graded sha), ")
    if report.ok:
        return head + "OK"
    lines = [head + f"{len(report.problems)} PROBLEM(S) — hand-verify before crediting"]
    for p in report.problems[:12]:
        lines.append(f"    ! {p.kind}: {p.detail}")
    if len(report.problems) > 12:
        lines.append(f"    … {len(report.problems) - 12} more")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """`python -m benchmarks.maintain_suite.ledger <dir> [<dir> ...] [--json]`:
    audit existing bench output dirs. Exit 1 if any has a problem."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    reports = [check_ledger(d) for d in args.dirs]
    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2))
    else:
        for r in reports:
            print(r.root)
            print(render(r))
    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
