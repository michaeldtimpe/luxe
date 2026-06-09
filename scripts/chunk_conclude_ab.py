#!/usr/bin/env python
"""Track B — chunk-conclude A/B harness for gitaudit deep mode.

Experiment plan: ~/.claude/plans/elegant-painting-mccarthy.md

Replays ONLY Stage 2 (the per-chunk pass) of gitaudit deep mode, reusing the FRESH
per-repo cached map (survey + chunk plan), so we can A/B chunk-prompt interventions
in isolation — no survey, no synthesis, no whole-repo rerun. Each (repo, arm, rep,
chunk) = exactly ONE `run_single` chunk pass; we classify whether it concluded
(emitted the report header / parseable JSON), is heuristic-salvageable (the shipped
free recovery), or is truly lost — and count findings, to guard against an arm that
concludes by SUPPRESSING findings.

Isolation choices (deliberate):
- Empty digest per chunk (no cross-chunk order dependence → identical input for chunk
  N across arms/reps). A shipped winner gets a real-digest "sentinel" re-check.
- BM25/symbol indices built ONCE per repo, restored after; identity asserted stable
  across arms so Arm-0 state can't leak into Arm-1.

Read-only w.r.t. the package + target repos (analysis runs through make_read_only_role).
Writes only scripts/out/chunk_conclude_ab.csv (atomic append) + raw dumps under
scripts/out/ccab/. Resumable: a done (repo,arm,rep,chunk) cell is skipped unless --force.

Usage (drive the protocol from the CLI; resumable so run in chunks):
  # Phase 1 — baseline band first:
  uv run python scripts/chunk_conclude_ab.py --repos deluxe,neo-llm-bench --arms A0 --reps 3
  # Phase 2 — arms:
  uv run python scripts/chunk_conclude_ab.py --repos deluxe,neo-llm-bench --arms A1,A2,A3 --reps 3
  uv run python scripts/chunk_conclude_ab.py --repos deluxe,neo-llm-bench --arms A4 --reps 2
  # Controls (after a winner is known): --repos launch-lights,whetstone --arms A0,<winner> --reps 2
"""
from __future__ import annotations

import argparse
import csv
import os
import time
from pathlib import Path

from luxe.agents import prompts
from luxe.gitkit import deep, health, store
from luxe.gitkit.runner import extract_report

OUT_CSV = Path(__file__).resolve().parent / "out" / "chunk_conclude_ab.csv"
DUMP_DIR = Path(__file__).resolve().parent / "out" / "ccab"
_CLONE_ROOT = Path.home() / ".luxe" / "sweep-clones"

# --- arm definitions: an appended CLAUSE to the live audit chunk hint (isolates the
# intervention as an additive change) + an optional per-turn token cap override. ---
_A1 = ("\n\nORDERING (mandatory): output the `# Repository audit` title line as the "
       "VERY FIRST line of your response, before any analysis or tool reflection. "
       "Fill `**Findings: N**` and the two sections afterward; the title must exist "
       "first so the report is always recoverable even if you run long.")
_A2 = ("\n\nDISCIPLINE (mandatory): make ONE brief pass over these files, then STOP. "
       "Do not re-read, re-verify, or second-guess. Commit your findings once and "
       "write the report immediately — do not keep exploring.")
ARMS = {
    "A0": {"clause": "", "cap": deep._CHUNK_MAX_TOKENS},          # baseline
    "A1": {"clause": _A1, "cap": deep._CHUNK_MAX_TOKENS},         # header-first only
    "A2": {"clause": _A2, "cap": deep._CHUNK_MAX_TOKENS},         # commit-now only
    "A3": {"clause": _A1 + _A2, "cap": deep._CHUNK_MAX_TOKENS},   # combined
    "A4": {"clause": "", "cap": 8192},                           # forced-early (knob)
}

_FIELDS = ["repo", "arm", "rep", "chunk_index", "label", "est_tokens", "header_bool",
           "parsed_bool", "heuristic_n", "findings", "outcome", "out_chars",
           "completion_tokens", "wall_s"]


def _find(name: str) -> Path | None:
    for b in [Path("~/Downloads").expanduser() / name, _CLONE_ROOT / name]:
        if (b / ".git").exists():
            return b
    return None


def _done_cells(force: bool) -> set[tuple]:
    if force or not OUT_CSV.is_file():
        return set()
    done = set()
    with OUT_CSV.open() as fh:
        for r in csv.DictReader(fh):
            done.add((r["repo"], r["arm"], int(r["rep"]), int(r["chunk_index"])))
    return done


def _classify(text: str) -> tuple[str, int, bool, bool]:
    """Return (outcome, findings, header_bool, parsed_bool). outcome in:
    concluded (header/JSON in the chunk's OWN output — the prevention win),
    salvageable (heuristic≥1 — the shipped free recovery), unparsed (truly lost)."""
    parsed = deep.parse_chunk_notes(text)
    header = deep._has_report_header(text, "gitaudit")
    if parsed:
        return "concluded", len(parsed.get("findings") or []), header, True
    if header:
        sliced = extract_report(text, "gitaudit")
        return "concluded", len(deep._heuristic_findings(sliced)), True, False
    heur = deep._heuristic_findings(text)
    if heur:
        return "salvageable", len(heur), False, False
    return "unparsed", 0, False, False


def main() -> None:
    from luxe import search as search_mod
    from luxe import symbols as symbols_mod
    from luxe.agents.single import run_single
    from luxe.backend import Backend
    from luxe.cli import _detect_languages_for_repo
    from luxe.config import load_config
    from luxe.mcp.server import make_read_only_role
    from luxe.tools.fs import get_repo_root, set_repo_root

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repos", default="deluxe,neo-llm-bench")
    ap.add_argument("--arms", default="A0,A1,A2,A3")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0,
                    help="cap chunks per repo (0=all) — for a cheap dry-run")
    ap.add_argument("--config", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    repos = [r for r in args.repos.split(",") if r]
    arms = [a for a in args.arms.split(",") if a]
    for a in arms:
        if a not in ARMS:
            raise SystemExit(f"unknown arm {a}; choose from {list(ARMS)}")
    done = _done_cells(args.force)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    new_file = not OUT_CSV.is_file() or args.force
    cfg = load_config(args.config or "configs/chat.yaml")

    fh = OUT_CSV.open("w" if args.force else "a", newline="", buffering=1)
    writer = csv.DictWriter(fh, fieldnames=_FIELDS)
    if new_file:
        writer.writeheader(); fh.flush(); os.fsync(fh.fileno())

    prev_root = get_repo_root()
    n_run = n_skip = 0
    try:
        for repo in repos:
            p = _find(repo)
            if not p:
                print(f"  {repo}: no local clone — skip"); continue
            target = str(p.resolve())
            head = health.current_head(target)
            cached = deep.load_map(target, head=head)
            if not cached:
                st = deep.map_status(target, head=head)
                print(f"  {repo}: map not FRESH ({st.state.name}) — skip "
                      f"(rebuild via `luxe gitaudit {repo} --rebuild-map` first)")
                continue
            chunks = [c for c in cached["chunks"] if c.files]
            if args.limit:
                chunks = chunks[:args.limit]
            survey_notes = cached["survey_notes"]
            languages = _detect_languages_for_repo(target)
            model = cfg.model_for_slot("chat")
            backend = Backend(base_url=cfg.omlx_base_url, model=model)
            base_role = make_read_only_role(cfg.role("monolith"))
            win = deep.deep_window(base_role)

            # indices built ONCE per repo, restored after; identity asserted stable.
            set_repo_root(target)
            search_mod.set_index(search_mod.build_bm25_index(target))
            symbols_mod.set_index(symbols_mod.build_symbol_index(target))
            idn = (id(search_mod._index), id(symbols_mod._index))
            print(f"· {repo}: {len(chunks)} chunks, win={win}, model={model}")

            for arm in arms:
                cap = ARMS[arm]["cap"]
                clause = ARMS[arm]["clause"]
                role = base_role.model_copy(update={"num_ctx": win,
                                                    "max_tokens_per_turn": cap})
                hint = prompts.GIT_AUDIT_CHUNK_HINT + clause
                for rep in range(1, args.reps + 1):
                    for c in chunks:
                        cell = (repo, arm, rep, c.index)
                        if cell in done:
                            n_skip += 1; continue
                        assert (id(search_mod._index), id(symbols_mod._index)) == idn, \
                            "index identity drifted across arms — isolation broken"
                        extra = (f"<survey_notes>\n{survey_notes}\n</survey_notes>\n\n"
                                 f"{deep._digest_block(deep.empty_digest())}\n\n"
                                 f"{deep._chunk_block(c, len(chunks))}")
                        goal = (f"Analyze chunk {c.index + 1} of {len(chunks)} of this "
                                f"repository.\n\n{hint}")
                        t0 = time.time()
                        try:
                            res = run_single(
                                backend, role, goal=goal, task_type="review",
                                languages=languages, extra_context=extra,
                                phase="chat",
                                run_id=f"ccab-{repo}-{arm}-r{rep}-c{c.index + 1}")
                            text = (getattr(res, "final_text", "") or "").strip()
                            ctok = int(getattr(res, "completion_tokens", 0) or 0)
                            wall = round(float(getattr(res, "wall_s", time.time() - t0)), 1)
                        except Exception as e:  # never lose the run on one bad cell
                            text, ctok, wall = f"(error: {e})", 0, round(time.time() - t0, 1)
                        outcome, findings, hdr, parsed_b = _classify(text)
                        heur = len(deep._heuristic_findings(text))
                        dd = DUMP_DIR / repo / arm / f"r{rep}"
                        dd.mkdir(parents=True, exist_ok=True)
                        (dd / f"chunk-{c.index + 1:02d}.md").write_text(text or "(no output)")
                        writer.writerow({
                            "repo": repo, "arm": arm, "rep": rep, "chunk_index": c.index,
                            "label": c.label, "est_tokens": c.est_tokens,
                            "header_bool": int(hdr), "parsed_bool": int(parsed_b),
                            "heuristic_n": heur, "findings": findings, "outcome": outcome,
                            "out_chars": len(text), "completion_tokens": ctok,
                            "wall_s": wall})
                        fh.flush(); os.fsync(fh.fileno())
                        n_run += 1
                        print(f"  {repo} {arm} r{rep} c{c.index + 1}/{len(chunks)} "
                              f"[{c.label}] -> {outcome} ({findings}f, {wall}s)")
    finally:
        fh.close()
        if prev_root is not None:
            set_repo_root(prev_root)
        else:
            from luxe.tools.fs import set_repo_root as _srr  # noqa
        search_mod.reset_index(); symbols_mod.reset_index()
    print(f"\ndone: ran={n_run} skipped={n_skip} -> {OUT_CSV}")


if __name__ == "__main__":
    main()
