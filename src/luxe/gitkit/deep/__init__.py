"""gitkit DEEP MODE — staged map-reduce analysis for large repos.

The single-pass runner (`runner.run_git_report`) does ONE `run_single` pass over
a whole repo. That packaging cannot scale: on large repos the model enters a
repetition loop, blows the report token budget, and truncates mid-report even
though it found real issues. Deep mode fixes the *packaging* by staging the work
as multiple sequential read-only `run_single` passes orchestrated here in Python
(the `compare/run_pair.py` precedent — NOT the retired in-agent swarm/phased
modes; each pass is still one mono call with no in-agent repair loop):

  Stage 0  Survey   — deterministic repo map + one LLM pass → architectural
                      hypothesis (cached per repo under `map/`).
  Stage 1  Plan     — deterministic, token-budgeted chunker (cached under `map/`)
                      + an estimate and a large-repo confirmation gate.
  Stage 2  Analyze  — one pass per chunk → COMPACT structured notes + a running
                      structured cross-reference digest (hard-ceiling compacted).
  Stage 3  Synthesis— one pass over the AGGREGATE notes (NOT raw files) → the
                      consolidated report in the required gitkit shape, merging
                      duplicates and re-rating severity globally. A 2-level
                      reduce fallback triggers proactively if notes overflow.

Per-repo persistence (the user's "each mapped repo gets its own folder"): the
survey map + chunk plan live under `~/.luxe/reports/<repo_hash>/map/`, keyed by
HEAD, so a large repo is surveyed/chunked ONCE and reused across kinds/re-runs.
Per-run notes + the final digest live in a sibling `<kind>-<ts>-<rand>.work/`.

Package layout (split from the single `deep.py` along its stages):
  chunking.py  window/footprint gate, file enumeration, chunk partition,
               framing picker, wall estimate (Stages 0/1, deterministic)
  mapcache.py  the HEAD-keyed `map/` cache, blob/working-tree shas, the
               notes cache, and the pure incremental planner
  digest.py    chunk-note parsing, the cross-reference digest, confidence,
               merge/compaction, the 2-level synthesis reduce
  salvage.py   recovery of unpackaged output (ramble detector,
               transcription passes, heuristic finding salvage)
  render.py    pure-data extra_context blocks + deterministic report render
  __init__.py  `run_deep_report` (the orchestration) and its tunables; it
               re-exports the submodule names that something reads as
               `deep.X` (src, tests, scripts) or that the orchestration
               uses. A name only a submodule reads is imported from that
               submodule (pruned 2026-10: 46 re-exports nothing read).

`run_deep_report` lives HERE, not in a submodule, so its module-global
lookups (`_CONTENT_BUDGET_FRAC`, `enumerate_files`, …) resolve in this
namespace — which is what `monkeypatch.setattr(deep, ...)` patches.

All directive strings live in `agents/prompts.py` (gitkit.sdd Forbids inline
prompts). This package owns only orchestration + deterministic data shaping.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from luxe.agents import prompts
from luxe.cancel import ChatCancelled, raise_if_cancelled
from luxe.context import estimate_tokens
from luxe.gitkit import health
from luxe.gitkit import plan as plan_mod
from luxe.gitkit.kinds import _TITLES, extract_report, run_with_activity
from luxe.gitkit.output import finish_report


from luxe.gitkit.deep.chunking import (
    base_ctx as base_ctx,
    build_chunks as build_chunks,
    Chunk as Chunk,
    _CHUNK_S as _CHUNK_S,
    _DEEP_TRIGGER_FRAC as _DEEP_TRIGGER_FRAC,
    deep_window as deep_window,
    enumerate_files as enumerate_files,
    estimate_run as estimate_run,
    file_recs_for as file_recs_for,
    FileRec as FileRec,
    framing_files as framing_files,
    _norm_recent as _norm_recent,
    should_use_deep as should_use_deep,
    _SURVEY_S as _SURVEY_S,
    _SYNTH_S as _SYNTH_S,
)

from luxe.gitkit.deep.digest import (
    compact_digest as compact_digest,
    confidence_of as confidence_of,
    empty_digest as empty_digest,
    parse_chunk_notes as parse_chunk_notes,
    _reduce_findings as _reduce_findings,
    _SYNTH_REDUCE_FRAC as _SYNTH_REDUCE_FRAC,
    update_digest as update_digest,
)

from luxe.gitkit.deep.salvage import (
    _clean_note as _clean_note,
    _format_final_report as _format_final_report,
    _has_report_header as _has_report_header,
    _heuristic_findings as _heuristic_findings,
    _looks_rambly as _looks_rambly,
    _plan_extract_pass as _plan_extract_pass,
    _SEV_LINE_RE as _SEV_LINE_RE,
)

from luxe.gitkit.deep.render import (
    _chunk_block as _chunk_block,
    _digest_block as _digest_block,
    _framing_block as _framing_block,
    _notes_block as _notes_block,
    _prior_findings_block as _prior_findings_block,
    _render_report as _render_report,
)

from luxe.gitkit.deep.mapcache import (
    _age_str as _age_str,
    _atomic_write_text as _atomic_write_text,
    _cacheable as _cacheable,
    CacheDecision as CacheDecision,
    chunk_note_is_valid as chunk_note_is_valid,
    fold_contribution as fold_contribution,
    fresh_survey_notes as fresh_survey_notes,
    git_file_shas as git_file_shas,
    load_chunk_note as load_chunk_note,
    load_map as load_map,
    make_baseline as make_baseline,
    _map_dir as _map_dir,
    map_status as map_status,
    MapState as MapState,
    MapStatus as MapStatus,
    _new_work_dir as _new_work_dir,
    _notes_dir as _notes_dir,
    plan_incremental as plan_incremental,
    save_chunk_note as save_chunk_note,
    save_map as save_map,
    worktree_file_shas as worktree_file_shas,
)


# --- tuning constants (per-stage wall fit from the 46-repo sweep) ------------

# Fraction of the effective context window reserved for file *content* the agent
# reads per chunk (leaves headroom for the prompt + injected map/digest + the
# report output + tool round-trips).
_CONTENT_BUDGET_FRAC = 0.55
# Hard ceiling for the running cross-reference digest, as a fraction of window.
_DIGEST_CEILING_FRAC = 0.15
# Generation headroom. The champion writes its whole analysis as prose in the
# final message, so a chunk pass fills whatever cap it is given; _CHUNK_MAX_TOKENS
# bounds that ramble (the extract pass recovers findings from it). _DEEP_MAX_TOKENS
# gives the synthesis report room beyond the single-pass GITKIT_MAX_TOKENS.
_CHUNK_MAX_TOKENS = 16384
_DEEP_MAX_TOKENS = 24576


@dataclass
class PassTiming:
    """One per-pass wall-clock record. Captured at the `_pass` choke point so every
    stage (survey / chunk-N / synthesis / format / reduce-N) is measured uniformly.
    This is the raw calibration dataset behind a future window/size-aware estimator
    that would replace the flat `_SECONDS_PER_CHUNK` constant. `started_at` (epoch
    seconds) preserves the timeline so later analysis can spot overnight pauses /
    machine sleep / model reloads. Chunk-only fields are enriched at the call site."""
    label: str
    kind: str
    wall_s: float
    completion_tokens: int
    steps: int
    tool_calls_total: int
    window: int
    started_at: int = 0
    est_tokens: int = 0     # chunk passes only
    loc: int = 0            # chunk passes only
    n_files: int = 0        # chunk passes only
    aborted: bool = False   # the pass ended in a backend/loop abort

    def to_dict(self) -> dict:
        return asdict(self)


# --- per-kind directive maps (strings stay in agents/prompts.py) ------------

# Two kinds. gitaudit emits a markdown audit report per chunk (bugs/security +
# structural improvements) recovered/packaged like the old review path; gitchange
# emits per-chunk steps (markdown, recovered to JSON) that accumulate in the `steps`
# digest bucket and are consolidated into one ordered plan at synthesis. Both
# auto-route to deep on large repos; the chunk loop has a gitchange-specific
# step-recovery branch (a prose chunk → steps via the extract hint).
_CHUNK_HINTS = {
    "gitaudit": prompts.GIT_AUDIT_CHUNK_HINT,
    "gitchange": prompts.GIT_CHANGE_CHUNK_HINT,
    "gitaudit-diff": prompts.GIT_AUDIT_DIFF_CHUNK_HINT,
}
_SYNTH_HINTS = {
    "gitaudit": prompts.GIT_AUDIT_SYNTH_HINT,
    "gitchange": prompts.GIT_CHANGE_SYNTH_HINT,
    "gitaudit-diff": prompts.GIT_AUDIT_DIFF_SYNTH_HINT,
}


# --- orchestration ----------------------------------------------------------
#
# `run_deep_report` is the entry point; the stage helpers below it share one
# `_DeepRun` (the per-run state the stages used to share as closure locals).
# They live in THIS module on purpose: their global lookups
# (`_CONTENT_BUDGET_FRAC`, `enumerate_files`, …) must resolve here, where
# tests monkeypatch them.

@dataclass
class _DeepRun:
    """Per-run state threaded through the stages: the fixed inputs, the
    derived windows/roles, and the mutable digest / timings / work dir."""
    kind: str
    target: str
    task_type: str
    backend: Any
    languages: Any
    console: Any
    reader: Any
    cancel: Any
    run_single_fn: Any
    chunk_role: Any
    synth_role: Any
    synth_ctx_win: int
    ceiling: int
    head: str
    diff_mode: bool
    # Per-pass wall-clock telemetry, accumulated across every `pass_` call (B1/B2).
    timings: list[PassTiming] = field(default_factory=list)
    digest: dict = field(default_factory=dict)
    work_dir: Path | None = None
    current_shas: dict[str, str] = field(default_factory=dict)

    def emit(self, text: str) -> None:
        # NB: avoid a literal "[deep]" — Rich would parse it as a markup tag and
        # strip it. Use a plain "deep ·" prefix instead.
        self.console.print(f"[dim]· deep · {text}[/]")

    def pass_(self, goal: str, extra_context: str, label: str, role=None):
        """The ONE choke point for every model pass (survey / chunk-N /
        plan-extract / format / reduce-N / synthesis): one `run_single`
        under the activity display, timed into `timings`."""
        role = role or self.chunk_role
        start = int(time.time())
        res = run_with_activity(
            self.console, f"deep · {label}", self.cancel,
            lambda on_e, on_t: self.run_single_fn(
                self.backend, role, goal=goal, task_type=self.task_type,
                languages=self.languages, extra_context=extra_context,
                on_tool_event=on_e, on_token=on_t,
                phase="chat", run_id=f"gitkit-deep-{self.kind}-{label}"),
            update_prefix=f"deep · {label} · ")
        # Read only public result fields (getattr-guarded — stubs + backends that
        # populate steps/tool_calls_total differently both stay safe).
        self.timings.append(PassTiming(
            label=label, kind=self.kind,
            wall_s=round(float(getattr(res, "wall_s", 0.0) or 0.0), 3),
            completion_tokens=int(getattr(res, "completion_tokens", 0) or 0),
            steps=int(getattr(res, "steps", 0) or 0),
            tool_calls_total=int(getattr(res, "tool_calls_total", 0) or 0),
            window=int(getattr(role, "num_ctx", 0) or 0),
            started_at=start,
            aborted=bool(getattr(res, "aborted", False))))
        return res

    def compact_if_over(self) -> None:
        """Hold the running digest under its hard ceiling (merge-only)."""
        if estimate_tokens(json.dumps(self.digest)) > self.ceiling:
            self.digest = compact_digest(self.digest)

    def write_xref(self) -> None:
        if self.work_dir is not None:
            (self.work_dir / "xref.json").write_text(
                json.dumps(self.digest, indent=2))

    def record_chunk(self, c: Chunk, contribution: dict, n_timed: int) -> None:
        """The shared tail of a FRESHLY analyzed chunk, for every kind: cache
        its contribution (unless diff mode, or any of its passes aborted / it
        came back unanalyzed — `_cacheable`), then refresh the run's
        xref.json from the digest as it stands now."""
        if not self.diff_mode and _cacheable(contribution,
                                             self.timings[n_timed:]):
            save_chunk_note(
                self.target, self.kind, c, head=self.head,
                file_shas={r: self.current_shas.get(r, "") for r in c.files},
                contribution=contribution,
                wall_s=(self.timings[-1].wall_s if self.timings else 0.0))
        self.write_xref()

    def flag_unparsed(self, c: Chunk, contribution: dict, what: str) -> None:
        """Empty/unsalvageable output — never silently drop a chunk; record it
        so the report can flag the coverage gap."""
        label = f"chunk {c.index + 1} ({c.label}): {', '.join(c.files[:4])}" \
            + (" …" if len(c.files) > 4 else "")
        self.digest["unparsed_chunks"].append(label)
        contribution["unparsed"] = label
        self.emit(f"chunk {c.index + 1} produced no usable {what} "
                  "(empty/truncated) — flagged as unanalyzed")


def run_deep_report(
    kind: str,
    *,
    target: str,
    task_type: str,
    backend,
    role_cfg,
    languages,
    console,
    reader,
    summary,
    symbol_index=None,
    health_block: str = "",
    save: bool = True,
    verbose: bool = False,
    cancel=None,
    max_chunks: int | None = None,
    rebuild_map: bool = False,
    prior_report: str = "",
    mirror: bool = True,
    run_single_fn=None,
    chunks_override: list[Chunk] | None = None,
    chunk_extra_blocks: dict[int, str] | None = None,
    survey_notes_override: str | None = None,
    postprocess=None,
    extra_meta: dict | None = None,
    min_severity: str | None = None,
    no_incremental: bool = False,
) -> tuple[str, Path | None]:
    """Run the staged deep analysis. Returns (report_text, saved_path | None);
    ("", None) on cancel/decline. The caller (runner) owns target resolution,
    index build/restore, and model unload; this owns the staging.

    `run_single_fn` is injectable for tests (count passes with a stub); defaults
    to the real `run_single`.

    Diff mode (`gitaudit --base/--pr`) passes `chunks_override` (chunks built
    over the CHANGED files only) — that skips the survey pass entirely and
    neither reads nor writes the per-repo `map/` cache (gitkit.sdd diff-mode
    rules); `survey_notes_override` may opportunistically inject a FRESH
    whole-repo map's survey notes. `chunk_extra_blocks` appends a per-chunk
    pure-data block (the chunk-scoped `<change_diff>`) to that chunk's
    extra_context. `postprocess` (report→report) runs before save —
    deterministic tag-prior/caveat rendering. `extra_meta` merges into the
    saved report's frontmatter (base / merge_base).
    """
    if run_single_fn is None:
        from luxe.agents.single import run_single as run_single_fn

    # Three windows/caps, deliberately different (copies — never mutate the
    # shared role):
    #  - CHUNK passes run at the BASE window (deep_window) so chunks are small
    #    enough to cover all their files before the model truncates its analysis.
    #  - The SYNTHESIS pass runs at the EXPANDED window (num_ctx_max) since it is a
    #    single pass that must hold ALL the aggregated notes at once, with extra
    #    generation headroom for the consolidated report.
    eff_ctx = deep_window(role_cfg)
    synth_ctx_win = getattr(role_cfg, "num_ctx_max", 0) or eff_ctx
    run = _DeepRun(
        kind=kind, target=target, task_type=task_type, backend=backend,
        languages=languages, console=console, reader=reader, cancel=cancel,
        run_single_fn=run_single_fn,
        chunk_role=role_cfg.model_copy(update={
            "num_ctx": eff_ctx, "max_tokens_per_turn": _CHUNK_MAX_TOKENS}),
        synth_role=role_cfg.model_copy(update={
            "num_ctx": synth_ctx_win, "max_tokens_per_turn": _DEEP_MAX_TOKENS}),
        synth_ctx_win=synth_ctx_win,
        ceiling=int(eff_ctx * _DIGEST_CEILING_FRAC),
        head=health.current_head(target),
        diff_mode=chunks_override is not None)
    content_budget = max(1, int(eff_ctx * _CONTENT_BUDGET_FRAC))

    # --- Stages 0+1: survey + chunk plan (cached per repo, HEAD-keyed) -------
    planned = _plan_stage(
        run, summary=summary, symbol_index=symbol_index,
        health_block=health_block, content_budget=content_budget,
        rebuild_map=rebuild_map, no_incremental=no_incremental,
        chunks_override=chunks_override,
        survey_notes_override=survey_notes_override)
    if planned is None:
        return "", None
    survey_notes, chunks = planned
    chunks = _cap_chunks(run, chunks, max_chunks)

    contributions = _reuse_notes(run, chunks, no_incremental=no_incremental,
                                 rebuild_map=rebuild_map)
    if not _confirm_plan(run, chunks, contributions):
        console.print("[yellow]· cancelled.[/]")
        return "", None

    run.work_dir = _new_work_dir(target, kind) if save else None
    run.digest = empty_digest()

    # --- Stage 2: per-chunk analysis ----------------------------------------
    try:
        for c in chunks:
            if c.files:
                _analyze_chunk(run, c, len(chunks), contributions,
                               survey_notes=survey_notes,
                               extra_block=(chunk_extra_blocks or {}).get(c.index))
    except (ChatCancelled, KeyboardInterrupt):
        if run.work_dir is not None:
            run.write_xref()
            console.print(f"[yellow]· cancelled — partial notes saved to "
                          f"{run.work_dir}[/]")
        else:
            console.print("[yellow]· cancelled.[/]")
        return "", None

    # Final dedupe/merge before synthesis (also re-runs the merge globally).
    run.digest = compact_digest(run.digest, ceiling_tokens=0)

    # --- Stage 3: synthesis at the EXPANDED window (one pass over all notes) -
    report = _synthesize(run, survey_notes=survey_notes,
                         health_block=health_block, prior_report=prior_report,
                         save=save)
    if report is None:
        console.print("[yellow]· cancelled.[/]")
        return "", None
    if postprocess is not None:
        report = postprocess(report)

    total_wall_s, n_passes, avg_pass_s = _write_timing(run)
    work_dir = run.work_dir
    after = (f"[dim]· survey/chunk notes: {work_dir}[/]",) if work_dir else ()
    saved = finish_report(
        console, target=target, kind=kind, report=report, head=run.head,
        meta={"model": backend.model, "head": run.head, "repo": target,
              "mode": "deep", "chunks": len(chunks),
              "total_wall_s": total_wall_s, "n_passes": n_passes,
              "avg_pass_s": avg_pass_s, **(extra_meta or {})},
        save=save, mirror=mirror, verbose=verbose, min_severity=min_severity,
        stats_line=(f"[dim]· deep · {len(chunks)} chunks · {n_passes} passes · "
                    f"{total_wall_s:.1f}s[/]"),
        after_saved=after)
    return report, saved


def _handle_partial_map(run: _DeepRun, status: MapStatus) -> CacheDecision:
    """A damaged map (heavy file missing/corrupt) is ANNOUNCED, never silently
    equated with 'never mapped'. Interactive → ask rebuild/cancel; batch (no
    TTY) → log loudly and rebuild (never block)."""
    miss = ", ".join(status.missing) or "?"
    prior = (f"HEAD {status.head[:8] or '?'}, {status.n_chunks} chunks, "
             f"mapped {_age_str(status.mapped_at)} ago")
    if run.console.is_terminal:
        run.emit(f"map partial — prior map ({prior}); missing/corrupt: {miss}")
        ans = run.reader("  map partial — [Y] rebuild, [n] cancel: ").strip().lower()
        return CacheDecision.CANCEL if ans in ("n", "no") else CacheDecision.REBUILD
    run.emit(f"map partial ({miss}) — rebuilding (re-surveying)")
    return CacheDecision.REBUILD


def _incremental_replan(run: _DeepRun, status: MapStatus, *, summary,
                        symbol_index, content_budget: int,
                        ) -> tuple[str, list[Chunk]] | None:
    """INCREMENTAL RE-AUDIT: HEAD moved but the v2 breadcrumb carries blob
    shas — keep the survey + partition, prune deletions, append delta
    chunks, and let the sha-validated notes cache decide which chunks
    actually re-run. plan_incremental is pure; any rebuild trigger is logged
    loudly (never silently skipped). Returns (survey_notes, chunks) after
    re-saving the map in place, or None → full rebuild."""
    stale = load_map(run.target, head=run.head, allow_stale=True,
                     status=status)
    if stale is None:
        return None
    run.current_shas = worktree_file_shas(run.target)
    added = sorted(set(run.current_shas) - set(status.files))
    # FileRecs for the ADDED files only — not a whole-tree walk
    # that counts every file's lines to find a handful of adds.
    added_recs = file_recs_for(run.target, added,
                               recent=_norm_recent(summary), log=run.emit)
    plan = plan_incremental(
        old_files=status.files, new_files=run.current_shas,
        chunks=stale["chunks"], baseline=status.baseline,
        added_recs=added_recs, content_budget=content_budget,
        symbol_index=symbol_index, framing=stale["framing"])
    if plan.mode != "incremental":
        run.emit(f"incremental unavailable — {plan.reason}; "
                 "full rebuild (re-survey)")
        return None
    survey_notes = stale["survey_notes"]
    run.emit(f"incremental: HEAD {status.head[:8] or '?'} → "
             f"{(run.head or '?')[:8]} — {plan.reason}")
    save_map(run.target, head=run.head, survey_notes=survey_notes,
             chunks=plan.chunks, content_budget=content_budget,
             framing=stale["framing"], summary_render=summary.render(),
             files=run.current_shas, baseline=plan.baseline)
    return survey_notes, plan.chunks


def _plan_stage(run: _DeepRun, *, summary, symbol_index, health_block: str,
                content_budget: int, rebuild_map: bool, no_incremental: bool,
                chunks_override: list[Chunk] | None,
                survey_notes_override: str | None,
                ) -> tuple[str, list[Chunk]] | None:
    """Stages 0+1 → (survey_notes, chunks), or None when the operator
    cancelled (a partial-map prompt or the survey pass)."""
    if chunks_override is not None:
        # Diff mode: chunks cover the CHANGED files only. NO survey pass (diff
        # audits must be fast) and NO map/ reads or writes; a FRESH whole-repo
        # map's survey notes may be injected opportunistically by the caller.
        return (survey_notes_override or "(no survey — diff-scoped audit)",
                chunks_override)
    status = map_status(run.target, head=run.head)
    cached = None if rebuild_map else load_map(run.target, head=run.head,
                                               status=status)
    if not rebuild_map and status.state is MapState.PARTIAL:
        if _handle_partial_map(run, status) is CacheDecision.CANCEL:
            run.console.print("[yellow]· cancelled.[/]")
            return None
        # else fall through to re-survey (cached stays None)
    if cached:
        run.emit(f"reusing cached repo map (HEAD {run.head or '?'})")
        return cached["survey_notes"], cached["chunks"]
    if (not rebuild_map and not no_incremental
            and status.state is MapState.STALE
            and status.version >= 2 and status.files):
        replanned = _incremental_replan(run, status, summary=summary,
                                        symbol_index=symbol_index,
                                        content_budget=content_budget)
        if replanned is not None:
            return replanned
    # Full (re-)survey: one survey pass, then a fresh partition, saved.
    framing = framing_files(run.target)
    survey_ctx = (f"{health_block}\n\n<repo_map>\n{summary.render()}\n</repo_map>"
                  f"\n\n{_framing_block(framing)}")
    try:
        res = run.pass_(prompts.GIT_SURVEY_GOAL, survey_ctx, "survey")
    except (ChatCancelled, KeyboardInterrupt):
        run.console.print("[yellow]· cancelled.[/]")
        return None
    survey_notes = (getattr(res, "final_text", "") or "").strip() \
        or "(survey produced no notes)"
    files = enumerate_files(run.target, summary, log=run.emit)
    chunks = build_chunks(files, content_budget=content_budget,
                          symbol_index=symbol_index)
    save_map(run.target, head=run.head, survey_notes=survey_notes,
             chunks=chunks, content_budget=content_budget, framing=framing,
             summary_render=summary.render())
    return survey_notes, chunks


def _cap_chunks(run: _DeepRun, chunks: list[Chunk],
                max_chunks: int | None) -> list[Chunk]:
    """The --max-chunks safety valve (loud — no silent truncation)."""
    if max_chunks is None or len(chunks) <= max_chunks:
        return chunks
    dropped = chunks[max_chunks:]
    dropped_dirs = sorted({c.label for c in dropped})
    kept = chunks[:max_chunks]
    run.emit(f"--max-chunks={max_chunks}: analyzing {len(kept)} of "
             f"{len(kept) + len(dropped)} chunks; SKIPPING {len(dropped)} "
             f"(areas: {', '.join(dropped_dirs)})")
    return kept


def _reuse_notes(run: _DeepRun, chunks: list[Chunk], *, no_incremental: bool,
                 rebuild_map: bool) -> dict[int, dict]:
    """Sha-validated per-chunk notes reuse (incremental re-audit +
    crash-resume): a cached contribution is reused iff it covers exactly the
    chunk's files and every blob sha matches the CURRENT tree. Cached
    contributions are chunk INPUTS only — the digest is rebuilt from scratch
    every run and the synthesis always re-runs, so a stale finding cannot
    survive its source chunk's invalidation by construction."""
    contributions: dict[int, dict] = {}
    if not run.diff_mode:
        run.current_shas = run.current_shas or worktree_file_shas(run.target)
        if not no_incremental and not rebuild_map:
            for c in chunks:
                if not c.files:
                    continue
                note = load_chunk_note(run.target, run.kind, c.index)
                if chunk_note_is_valid(note, c, run.current_shas):
                    contributions[c.index] = note["contribution"]
    n_eff = sum(1 for c in chunks if c.files)
    if contributions:
        run.emit(f"incremental: reusing {len(contributions)}/{n_eff} cached chunk "
                 f"note(s) — {n_eff - len(contributions)} chunk pass(es) to run")
    return contributions


def _confirm_plan(run: _DeepRun, chunks: list[Chunk],
                  contributions: dict[int, dict]) -> bool:
    """Announce the estimate; large repos get an interactive [Y/n] gate.
    The survey pass has already completed by here (reused from the map cache
    OR freshly run), so the estimate covers the REMAINING chunk + synth work."""
    n_eff = sum(1 for c in chunks if c.files)
    est = estimate_run(n_eff - len(contributions), survey_cached=True)
    run.emit(f"plan: {est.line()}")
    # Confirmation gate — large repos only, interactive only.
    if est.large and run.console.is_terminal:
        ans = run.reader(f"  deep analysis: {est.line()}. Proceed? [Y/n]: ").strip().lower()
        if ans in ("n", "no"):
            return False
    return True


def _analyze_chunk(run: _DeepRun, c: Chunk, total: int,
                   contributions: dict[int, dict], *, survey_notes: str,
                   extra_block: str | None) -> None:
    """One chunk of Stage 2: fold a reused note, or run the chunk pass and
    route its output through the kind's recovery ladder."""
    if run.cancel is not None:
        raise_if_cancelled(run.cancel)
    if c.index in contributions:
        # Clean chunk: fold the cached contribution in chunk order,
        # exactly as the live path would have.
        fold_contribution(run.digest, contributions[c.index], c.index)
        run.compact_if_over()
        run.emit(f"chunk {c.index + 1}/{total} ({c.label}) — cached note reused")
        run.write_xref()
        return
    contribution: dict = {}
    n_timed = len(run.timings)     # every pass of THIS chunk lands after here
    run.emit(f"chunk {c.index + 1}/{total} ({c.label})")
    extra = (f"<survey_notes>\n{survey_notes}\n</survey_notes>\n\n"
             f"{_digest_block(run.digest, max_tokens=run.ceiling)}\n\n"
             f"{_chunk_block(c, total)}")
    if extra_block is not None:
        extra += f"\n\n{extra_block}"
    ask = prompts.GIT_CHUNK_ASK.format(n=c.index + 1, total=total)
    goal = f"{ask}\n\n{_CHUNK_HINTS[run.kind]}"
    res = run.pass_(goal, extra, f"chunk-{c.index + 1}")
    # Enrich the just-recorded chunk timing with its footprint — defensively
    # (a mid-pass throw would have raised before appending, so guard the
    # index + label match rather than blindly indexing timings[-1]).
    if run.timings and run.timings[-1].label == f"chunk-{c.index + 1}":
        run.timings[-1].est_tokens = c.est_tokens
        run.timings[-1].loc = c.loc
        run.timings[-1].n_files = len(c.files)
    text = (getattr(res, "final_text", "") or "").strip()
    parsed = parse_chunk_notes(text)
    if run.work_dir is not None:
        (run.work_dir / f"chunk-{c.index + 1:02d}.md").write_text(
            text or "(no output)")
    if run.kind == "gitchange":
        _analyze_change_chunk(run, c, text, parsed, contribution)
    else:
        _analyze_audit_chunk(run, c, text, parsed, contribution)
    run.record_chunk(c, contribution, n_timed)


def _analyze_change_chunk(run: _DeepRun, c: Chunk, text: str,
                          parsed: dict | None, contribution: dict) -> None:
    """gitchange chunks emit a CONCISE MARKDOWN step list (a JSON-only chunk
    contract makes the champion ramble past the cap without concluding —
    confirmed on luxe). Recover gitplan/v1 steps via the transcription pass;
    a chunk that already emitted JSON steps is used directly. Three outcomes:
    steps recorded / analyzed-but-no-steps / unanalyzed (a genuine coverage
    gap — never silently dropped)."""
    steps_obj = parsed if (parsed and parsed.get("steps")) else None
    analyzed = parsed is not None      # chunk emitted parseable JSON
    if steps_obj is None and text and not parsed:
        recovered = parse_chunk_notes(
            _plan_extract_pass(text, pass_fn=run.pass_, role=run.chunk_role))
        if recovered is not None:       # transcription produced valid JSON
            analyzed = True
            if recovered.get("steps"):
                steps_obj = recovered
    if steps_obj and steps_obj.get("steps"):
        update_digest(run.digest, steps_obj, c.index)
        contribution["parsed"] = steps_obj
        run.compact_if_over()
        run.emit(f"chunk {c.index + 1}: {len(steps_obj['steps'])} "
                 "step(s) recorded")
    elif analyzed:
        run.emit(f"chunk {c.index + 1}: no structural steps in these files")
    else:
        run.flag_unparsed(c, contribution, "steps")


def _analyze_audit_chunk(run: _DeepRun, c: Chunk, text: str,
                         parsed: dict | None, contribution: dict) -> None:
    """gitaudit / gitaudit-diff: structured notes go into the digest; a
    markdown conclusion (or the common rambling prose) is packaged into a
    CLEAN note — the model won't self-package (gitkit.sdd)."""
    if parsed:
        update_digest(run.digest, parsed, c.index)
        contribution["parsed"] = parsed
        run.compact_if_over()
        return
    if _has_report_header(text, run.kind):
        # The model concluded with the required header itself — slice off
        # any leading monologue and keep the conclusion.
        note_src = extract_report(text, run.kind)
    elif text:
        # The common champion case: the final message is a long file-by-file
        # analysis that never reaches a structured conclusion. The findings
        # ARE in that prose; _clean_note recovers them (transcription pass →
        # heuristic). This is load-bearing: the model won't self-package.
        note_src = text
    else:
        run.flag_unparsed(c, contribution, "findings")
        return
    # Always store a CLEAN note (as-is if already clean, else a
    # transcription pass, else heuristic finding lines) so the final
    # report can be assembled without any rambly text.
    clean, note_source = _clean_note(note_src, run.kind, pass_fn=run.pass_,
                                     role=run.chunk_role, log=run.emit)
    if not clean:
        run.flag_unparsed(c, contribution, "findings")  # nothing salvageable
        return
    run.digest["markdown_notes"].append(
        {"chunk": c.index, "label": c.label, "md": clean,
         "source": note_source})
    contribution["note"] = {"label": c.label, "md": clean,
                            "source": note_source}
    run.emit(f"chunk {c.index + 1}: findings recorded")


def _synthesize(run: _DeepRun, *, survey_notes: str, health_block: str,
                prior_report: str, save: bool) -> str | None:
    """Stage 3: (2-level reduce if the notes overflow) → ONE synthesis pass
    at the expanded window → the packaged report. None if cancelled."""
    notes_tokens = estimate_tokens(json.dumps(run.digest))
    if notes_tokens > _SYNTH_REDUCE_FRAC * run.synth_ctx_win:
        run.emit(f"aggregate notes large ({notes_tokens} tok) — 2-level reduce")
        run.digest = _reduce_findings(run.digest, eff_ctx=run.synth_ctx_win,
                                      pass_fn=run.pass_, log=run.emit,
                                      role=run.synth_role)

    synth_ctx = (f"{health_block}\n\n<survey_notes>\n{survey_notes}\n</survey_notes>"
                 f"\n\n{_notes_block(run.digest)}")
    if prior_report:
        synth_ctx += f"\n\n{_prior_findings_block(prior_report)}"
    synth_goal = f"{prompts.GIT_SYNTH_ASK}\n\n{_SYNTH_HINTS[run.kind]}"
    try:
        res = run.pass_(synth_goal, synth_ctx, "synthesis", role=run.synth_role)
    except (ChatCancelled, KeyboardInterrupt):
        return None

    synth_text = (getattr(res, "final_text", "") or "").strip()
    if run.kind == "gitchange":
        # gitchange emits a structured JSON plan, not a markdown report. Robustness
        # ladder: parse the synthesis JSON → on a prose synthesis, a transcription
        # recovery pass (its own draft → JSON) → finally the aggregated per-chunk
        # digest steps (Python packaging never rambles, so a valid plan is always
        # produced). Then save plan.json + render the markdown deterministically.
        report, _ = plan_mod.finalize_and_save(
            run.target, run.head, synth_text,
            extract_fn=lambda draft: _plan_extract_pass(
                draft, pass_fn=run.pass_, role=run.synth_role),
            fallback_steps=run.digest.get("steps"), title=_TITLES["gitchange"],
            save=save)
        return report
    return _package_synthesis(run, synth_text)


def _package_synthesis(run: _DeepRun, synth_text: str) -> str:
    """Use the LLM synthesis ONLY if it came back clean. The champion narrates
    its consolidation into the report, so when it doesn't: try a strict
    transcription pass, and if THAT is still rambly, assemble the report
    DETERMINISTICALLY from the (already-cleaned) per-chunk notes — Python
    packaging never rambles, so a clean, complete report is guaranteed."""
    report = extract_report(synth_text, run.kind)
    if not report or _looks_rambly(report):
        cleaned = (_format_final_report(synth_text, run.kind, pass_fn=run.pass_,
                                        role=run.synth_role)
                   if _looks_rambly(synth_text) else report)
        if cleaned and not _looks_rambly(cleaned):
            run.emit("synthesis verbose — formatted a clean report")
            report = cleaned
        else:
            run.emit("synthesis unclean — assembling report deterministically")
            report = _render_report(run.digest, run.kind)
    return report or _render_report(run.digest, run.kind) or "(no report produced)"


def _write_timing(run: _DeepRun) -> tuple[float, int, float]:
    """Timing telemetry: raw per-pass records + cheap aggregates (B3/B4).
    Returns (total_wall_s, n_passes, avg_pass_s); writes timing.json when
    the run has a work dir."""
    total_wall_s = round(sum(t.wall_s for t in run.timings), 3)
    n_passes = len(run.timings)
    avg_pass_s = round(total_wall_s / n_passes, 3) if n_passes else 0.0
    if run.work_dir is not None:
        # The `passes` list is the raw, append-only record (ages best); the
        # aggregates are convenience derivations of it.
        (run.work_dir / "timing.json").write_text(json.dumps({
            "kind": run.kind, "head": run.head, "n_passes": n_passes,
            "total_wall_s": total_wall_s, "avg_pass_s": avg_pass_s,
            "passes": [t.to_dict() for t in run.timings],
        }, indent=2))
    return total_wall_s, n_passes, avg_pass_s
