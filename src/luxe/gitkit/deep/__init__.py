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
               re-exports every name the single module had, so
               `from luxe.gitkit import deep; deep.X` keeps working.

`run_deep_report` lives HERE, not in a submodule, so its module-global
lookups (`_CONTENT_BUDGET_FRAC`, `enumerate_files`, …) resolve in this
namespace — which is what `monkeypatch.setattr(deep, ...)` patches.

All directive strings live in `agents/prompts.py` (gitkit.sdd Forbids inline
prompts). This package owns only orchestration + deterministic data shaping.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from luxe.agents import prompts
from luxe.cancel import ChatCancelled, raise_if_cancelled
from luxe.context import estimate_tokens


from luxe.gitkit.deep.chunking import (
    base_ctx as base_ctx,
    build_chunks as build_chunks,
    _CHARS_PER_TOKEN as _CHARS_PER_TOKEN,
    Chunk as Chunk,
    _CHUNK_S as _CHUNK_S,
    _DEEP_TRIGGER_FRAC as _DEEP_TRIGGER_FRAC,
    deep_window as deep_window,
    DeepEstimate as DeepEstimate,
    _ENTRY_STEMS as _ENTRY_STEMS,
    enumerate_files as enumerate_files,
    estimate_repo_tokens as estimate_repo_tokens,
    estimate_run as estimate_run,
    _file_priority as _file_priority,
    file_recs_for as file_recs_for,
    FileRec as FileRec,
    framing_files as framing_files,
    _FRAMING_LIMIT as _FRAMING_LIMIT,
    _FRAMING_PATTERNS as _FRAMING_PATTERNS,
    _FRAMING_RE as _FRAMING_RE,
    _is_visible as _is_visible,
    _LARGE_CONFIRM_CHUNKS as _LARGE_CONFIRM_CHUNKS,
    _MAX_CHUNK_SYMBOLS as _MAX_CHUNK_SYMBOLS,
    _norm_recent as _norm_recent,
    _pick_framing as _pick_framing,
    _PRIORITY_SUBSTRINGS as _PRIORITY_SUBSTRINGS,
    should_use_deep as should_use_deep,
    _SURVEY_S as _SURVEY_S,
    _symbols_by_path as _symbols_by_path,
    _symbols_for as _symbols_for,
    _SYNTH_S as _SYNTH_S,
)

from luxe.gitkit.deep.digest import (
    compact_digest as compact_digest,
    confidence_of as confidence_of,
    _DEEP_KEYS as _DEEP_KEYS,
    empty_digest as empty_digest,
    _evidence_keys as _evidence_keys,
    _finding_chunks as _finding_chunks,
    _finding_key as _finding_key,
    _JSON_FENCE_RE as _JSON_FENCE_RE,
    _MAX_EVIDENCE_PER_FINDING as _MAX_EVIDENCE_PER_FINDING,
    _merge_evidence as _merge_evidence,
    _merge_into as _merge_into,
    parse_chunk_notes as parse_chunk_notes,
    _reduce_findings as _reduce_findings,
    _sev_rank as _sev_rank,
    _SEVERITY_RANK as _SEVERITY_RANK,
    _SOURCE_RANK as _SOURCE_RANK,
    _SYNTH_REDUCE_FRAC as _SYNTH_REDUCE_FRAC,
    update_digest as update_digest,
)

from luxe.gitkit.deep.salvage import (
    _BOLD_FILE_RE as _BOLD_FILE_RE,
    _clean_note as _clean_note,
    _FILE_LINE_RE as _FILE_LINE_RE,
    _FILE_REF_RE as _FILE_REF_RE,
    _FINDING_HEADING_RE as _FINDING_HEADING_RE,
    _format_final_report as _format_final_report,
    _has_report_header as _has_report_header,
    _heuristic_findings as _heuristic_findings,
    _looks_rambly as _looks_rambly,
    _NON_FINDING_RE as _NON_FINDING_RE,
    _NUM_BOLD_RE as _NUM_BOLD_RE,
    _NUM_PLAIN_RE as _NUM_PLAIN_RE,
    _plan_extract_pass as _plan_extract_pass,
    _RAMBLE_MARKERS as _RAMBLE_MARKERS,
    _REPORT_BULLET_RE as _REPORT_BULLET_RE,
    _SEV_LEAD_RE as _SEV_LEAD_RE,
    _SEV_LINE_RE as _SEV_LINE_RE,
    _SEV_WORD_RE as _SEV_WORD_RE,
)

from luxe.gitkit.deep.render import (
    _chunk_block as _chunk_block,
    _digest_block as _digest_block,
    _framing_block as _framing_block,
    _index_entries as _index_entries,
    _INDEX_LINE_CHARS as _INDEX_LINE_CHARS,
    _notes_block as _notes_block,
    _prior_findings_block as _prior_findings_block,
    _render_report as _render_report,
    _strip_report_header as _strip_report_header,
)

from luxe.gitkit.deep.mapcache import (
    _age_str as _age_str,
    _atomic_write_text as _atomic_write_text,
    _cacheable as _cacheable,
    CacheDecision as CacheDecision,
    chunk_note_is_valid as chunk_note_is_valid,
    fold_contribution as fold_contribution,
    git_file_shas as git_file_shas,
    _HASH_BATCH as _HASH_BATCH,
    IncrementalPlan as IncrementalPlan,
    load_chunk_note as load_chunk_note,
    load_map as load_map,
    make_baseline as make_baseline,
    _map_dir as _map_dir,
    map_status as map_status,
    MapState as MapState,
    MapStatus as MapStatus,
    _MAX_CHUNK_GROWTH_FRAC as _MAX_CHUNK_GROWTH_FRAC,
    _MAX_DELTA_CHUNKS as _MAX_DELTA_CHUNKS,
    _MAX_DELTA_TOKENS_FRAC as _MAX_DELTA_TOKENS_FRAC,
    _MAX_FILE_CHURN_FRAC as _MAX_FILE_CHURN_FRAC,
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
    from luxe.gitkit import health
    from luxe.gitkit.runner import _activity_callbacks

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
    chunk_role = role_cfg.model_copy(
        update={"num_ctx": eff_ctx, "max_tokens_per_turn": _CHUNK_MAX_TOKENS})
    synth_role = role_cfg.model_copy(
        update={"num_ctx": synth_ctx_win, "max_tokens_per_turn": _DEEP_MAX_TOKENS})
    content_budget = max(1, int(eff_ctx * _CONTENT_BUDGET_FRAC))
    ceiling = int(eff_ctx * _DIGEST_CEILING_FRAC)
    head = health.current_head(target)
    # Per-pass wall-clock telemetry, accumulated across every `_pass` call (B1/B2).
    timings: list[PassTiming] = []

    def _emit(text: str) -> None:
        # NB: avoid a literal "[deep]" — Rich would parse it as a markup tag and
        # strip it. Use a plain "deep ·" prefix instead.
        console.print(f"[dim]· deep · {text}[/]")

    def _pass(goal: str, extra_context: str, label: str, role=None):
        role = role or chunk_role
        start = int(time.time())
        if console.is_terminal:
            with console.status(f"[dim]deep · {label}…[/]", spinner="dots") as st:
                on_e, on_t = _activity_callbacks(
                    lambda t: st.update(f"[dim]deep · {label} · {t}[/]"), cancel=cancel)
                res = run_single_fn(
                    backend, role, goal=goal, task_type=task_type,
                    languages=languages, extra_context=extra_context,
                    on_tool_event=on_e, on_token=on_t,
                    phase="chat", run_id=f"gitkit-deep-{kind}-{label}")
        else:
            on_e, on_t = _activity_callbacks(lambda t: None, cancel=cancel)
            res = run_single_fn(
                backend, role, goal=goal, task_type=task_type,
                languages=languages, extra_context=extra_context,
                on_tool_event=on_e, on_token=on_t,
                phase="chat", run_id=f"gitkit-deep-{kind}-{label}")
        # Read only public result fields (getattr-guarded — stubs + backends that
        # populate steps/tool_calls_total differently both stay safe).
        timings.append(PassTiming(
            label=label, kind=kind,
            wall_s=round(float(getattr(res, "wall_s", 0.0) or 0.0), 3),
            completion_tokens=int(getattr(res, "completion_tokens", 0) or 0),
            steps=int(getattr(res, "steps", 0) or 0),
            tool_calls_total=int(getattr(res, "tool_calls_total", 0) or 0),
            window=int(getattr(role, "num_ctx", 0) or 0),
            started_at=start,
            aborted=bool(getattr(res, "aborted", False))))
        return res

    def _handle_partial_map(status: MapStatus) -> CacheDecision:
        """A damaged map (heavy file missing/corrupt) is ANNOUNCED, never silently
        equated with 'never mapped'. Interactive → ask rebuild/cancel; batch (no
        TTY) → log loudly and rebuild (never block)."""
        miss = ", ".join(status.missing) or "?"
        prior = (f"HEAD {status.head[:8] or '?'}, {status.n_chunks} chunks, "
                 f"mapped {_age_str(status.mapped_at)} ago")
        if console.is_terminal:
            _emit(f"map partial — prior map ({prior}); missing/corrupt: {miss}")
            ans = reader("  map partial — [Y] rebuild, [n] cancel: ").strip().lower()
            return CacheDecision.CANCEL if ans in ("n", "no") else CacheDecision.REBUILD
        _emit(f"map partial ({miss}) — rebuilding (re-surveying)")
        return CacheDecision.REBUILD

    # --- Stages 0+1: survey + chunk plan (cached per repo, HEAD-keyed) -------
    current_shas: dict[str, str] = {}
    # `cached` means exactly one thing: the loaded map dict, or None. Skipping
    # the survey/save_map branch is a SEPARATE decision — diff mode and a
    # successful incremental replan both skip it without having loaded a map.
    # (These were once the same variable, with `cached = True` used as a
    # sentinel; that made `cached` unindexable-but-truthy and cost 4 mypy
    # errors.) Both must be bound before the branch: diff mode never reaches
    # the `else`, so leaving `cached` unassigned there is an UnboundLocalError
    # on the guard below.
    cached: dict | None = None
    skip_map_io = False
    if chunks_override is not None:
        # Diff mode: chunks cover the CHANGED files only. NO survey pass (diff
        # audits must be fast) and NO map/ reads or writes; a FRESH whole-repo
        # map's survey notes may be injected opportunistically by the caller.
        chunks = chunks_override
        survey_notes = survey_notes_override or "(no survey — diff-scoped audit)"
        framing = []
        skip_map_io = True  # no map read/write in diff mode
    else:
        status = map_status(target, head=head)
        cached = None if rebuild_map else load_map(target, head=head)
        if not rebuild_map and status.state is MapState.PARTIAL:
            if _handle_partial_map(status) is CacheDecision.CANCEL:
                console.print("[yellow]· cancelled.[/]")
                return "", None
            # else fall through to re-survey (cached stays None)
        if cached:
            _emit(f"reusing cached repo map (HEAD {head or '?'})")
            survey_notes = cached["survey_notes"]
            chunks = cached["chunks"]
            framing = cached["framing"]
        elif (not rebuild_map and not no_incremental
              and status.state is MapState.STALE
              and status.version >= 2 and status.files):
            # INCREMENTAL RE-AUDIT: HEAD moved but the v2 breadcrumb carries
            # blob shas — keep the survey + partition, prune deletions, append
            # delta chunks, and let the sha-validated notes cache decide which
            # chunks actually re-run. plan_incremental is pure; any rebuild
            # trigger is logged loudly (never silently skipped).
            stale = load_map(target, head=head, allow_stale=True)
            if stale is not None:
                current_shas = worktree_file_shas(target)
                added = sorted(set(current_shas) - set(status.files))
                # FileRecs for the ADDED files only — not a whole-tree walk
                # that counts every file's lines to find a handful of adds.
                added_recs = file_recs_for(target, added,
                                           recent=_norm_recent(summary),
                                           log=_emit)
                plan = plan_incremental(
                    old_files=status.files, new_files=current_shas,
                    chunks=stale["chunks"], baseline=status.baseline,
                    added_recs=added_recs, content_budget=content_budget,
                    symbol_index=symbol_index, framing=stale["framing"])
                if plan.mode == "incremental":
                    survey_notes = stale["survey_notes"]
                    chunks = plan.chunks
                    framing = stale["framing"]
                    _emit(f"incremental: HEAD {status.head[:8] or '?'} → "
                          f"{(head or '?')[:8]} — {plan.reason}")
                    save_map(target, head=head, survey_notes=survey_notes,
                             chunks=chunks, content_budget=content_budget,
                             framing=framing, summary_render=summary.render(),
                             files=current_shas, baseline=plan.baseline)
                    skip_map_io = True  # replanned in place; nothing to re-survey
                else:
                    _emit(f"incremental unavailable — {plan.reason}; "
                          "full rebuild (re-survey)")
    if cached is None and not skip_map_io:
        framing = framing_files(target)
        survey_ctx = (f"{health_block}\n\n<repo_map>\n{summary.render()}\n</repo_map>"
                      f"\n\n{_framing_block(framing)}")
        survey_goal = ("Survey the repository in the current working directory.\n\n"
                       + prompts.GIT_SURVEY_HINT)
        try:
            res = _pass(survey_goal, survey_ctx, "survey")
        except (ChatCancelled, KeyboardInterrupt):
            console.print("[yellow]· cancelled.[/]")
            return "", None
        survey_notes = (getattr(res, "final_text", "") or "").strip() \
            or "(survey produced no notes)"
        files = enumerate_files(target, summary, log=_emit)
        chunks = build_chunks(files, content_budget=content_budget,
                              symbol_index=symbol_index)
        save_map(target, head=head, survey_notes=survey_notes, chunks=chunks,
                 content_budget=content_budget, framing=framing,
                 summary_render=summary.render())

    # max-chunks safety valve (loud — no silent truncation).
    if max_chunks is not None and len(chunks) > max_chunks:
        dropped = chunks[max_chunks:]
        dropped_dirs = sorted({c.label for c in dropped})
        chunks = chunks[:max_chunks]
        _emit(f"--max-chunks={max_chunks}: analyzing {len(chunks)} of "
              f"{len(chunks) + len(dropped)} chunks; SKIPPING {len(dropped)} "
              f"(areas: {', '.join(dropped_dirs)})")

    # Sha-validated per-chunk notes reuse (incremental re-audit + crash-resume):
    # a cached contribution is reused iff it covers exactly the chunk's files
    # and every blob sha matches the CURRENT tree. Cached contributions are
    # chunk INPUTS only — the digest is rebuilt from scratch every run and the
    # synthesis always re-runs, so a stale finding cannot survive its source
    # chunk's invalidation by construction.
    contributions: dict[int, dict] = {}
    if chunks_override is None:
        current_shas = current_shas or worktree_file_shas(target)
        if not no_incremental and not rebuild_map:
            for c in chunks:
                if not c.files:
                    continue
                note = load_chunk_note(target, kind, c.index)
                if chunk_note_is_valid(note, c, current_shas):
                    contributions[c.index] = note["contribution"]
    n_eff = sum(1 for c in chunks if c.files)
    if contributions:
        _emit(f"incremental: reusing {len(contributions)}/{n_eff} cached chunk "
              f"note(s) — {n_eff - len(contributions)} chunk pass(es) to run")

    # The survey pass has already completed by here (reused from the map cache OR
    # freshly run above), so the estimate covers the REMAINING chunk + synth work.
    est = estimate_run(n_eff - len(contributions), survey_cached=True)
    _emit(f"plan: {est.line()}")

    # Confirmation gate — large repos only, interactive only.
    if est.large and console.is_terminal:
        ans = reader(f"  deep analysis: {est.line()}. Proceed? [Y/n]: ").strip().lower()
        if ans in ("n", "no"):
            console.print("[yellow]· cancelled.[/]")
            return "", None

    work_dir = _new_work_dir(target, kind) if save else None
    digest = empty_digest()
    from luxe.gitkit.runner import extract_report

    # --- Stage 2: per-chunk analysis ----------------------------------------
    chunk_hint = _CHUNK_HINTS[kind]
    try:
        for c in chunks:
            if not c.files:
                continue
            raise_if_cancelled(cancel) if cancel is not None else None
            if c.index in contributions:
                # Clean chunk: fold the cached contribution in chunk order,
                # exactly as the live path would have.
                fold_contribution(digest, contributions[c.index], c.index)
                if estimate_tokens(json.dumps(digest)) > ceiling:
                    digest = compact_digest(digest)
                _emit(f"chunk {c.index + 1}/{len(chunks)} ({c.label}) — "
                      "cached note reused")
                if work_dir is not None:
                    (work_dir / "xref.json").write_text(
                        json.dumps(digest, indent=2))
                continue
            contribution: dict = {}
            n_timed = len(timings)     # every pass of THIS chunk lands after here
            _emit(f"chunk {c.index + 1}/{len(chunks)} ({c.label})")
            extra = (f"<survey_notes>\n{survey_notes}\n</survey_notes>\n\n"
                     f"{_digest_block(digest, max_tokens=ceiling)}\n\n"
                     f"{_chunk_block(c, len(chunks))}")
            if chunk_extra_blocks and c.index in chunk_extra_blocks:
                extra += f"\n\n{chunk_extra_blocks[c.index]}"
            goal = (f"Analyze chunk {c.index + 1} of {len(chunks)} of this "
                    f"repository.\n\n{chunk_hint}")
            res = _pass(goal, extra, f"chunk-{c.index + 1}")
            # Enrich the just-recorded chunk timing with its footprint — defensively
            # (a mid-pass throw would have raised before appending, so guard the
            # index + label match rather than blindly indexing timings[-1]).
            if timings and timings[-1].label == f"chunk-{c.index + 1}":
                timings[-1].est_tokens = c.est_tokens
                timings[-1].loc = c.loc
                timings[-1].n_files = len(c.files)
            text = (getattr(res, "final_text", "") or "").strip()
            parsed = parse_chunk_notes(text)
            if work_dir is not None:
                (work_dir / f"chunk-{c.index + 1:02d}.md").write_text(
                    text or "(no output)")

            if kind == "gitchange":
                # gitchange chunks emit a CONCISE MARKDOWN step list (a JSON-only chunk
                # contract makes the champion ramble past the cap without concluding
                # — confirmed on luxe). Recover gitplan/v1 steps via the transcription
                # pass; a chunk that already emitted JSON steps is used directly. Three
                # outcomes: steps recorded / analyzed-but-no-steps / unanalyzed (a
                # genuine coverage gap — never silently dropped).
                steps_obj = parsed if (parsed and parsed.get("steps")) else None
                analyzed = parsed is not None      # chunk emitted parseable JSON
                if steps_obj is None and text and not parsed:
                    recovered = parse_chunk_notes(
                        _plan_extract_pass(text, pass_fn=_pass, role=chunk_role))
                    if recovered is not None:       # transcription produced valid JSON
                        analyzed = True
                        if recovered.get("steps"):
                            steps_obj = recovered
                if steps_obj and steps_obj.get("steps"):
                    update_digest(digest, steps_obj, c.index)
                    contribution["parsed"] = steps_obj
                    if estimate_tokens(json.dumps(digest)) > ceiling:
                        digest = compact_digest(digest)
                    _emit(f"chunk {c.index + 1}: {len(steps_obj['steps'])} "
                          "step(s) recorded")
                elif analyzed:
                    _emit(f"chunk {c.index + 1}: no structural steps in these files")
                else:
                    label = f"chunk {c.index + 1} ({c.label}): " \
                        + ", ".join(c.files[:4]) + (" …" if len(c.files) > 4 else "")
                    digest["unparsed_chunks"].append(label)
                    contribution["unparsed"] = label
                    _emit(f"chunk {c.index + 1} produced no usable steps "
                          "(empty/truncated) — flagged as unanalyzed")
                if chunks_override is None and _cacheable(contribution,
                                                          timings[n_timed:]):
                    save_chunk_note(
                        target, kind, c, head=head,
                        file_shas={r: current_shas.get(r, "")
                                   for r in c.files},
                        contribution=contribution,
                        wall_s=(timings[-1].wall_s if timings else 0.0))
                if work_dir is not None:
                    (work_dir / "xref.json").write_text(json.dumps(digest, indent=2))
                continue

            note_src = None
            if parsed:
                update_digest(digest, parsed, c.index)
                contribution["parsed"] = parsed
                if estimate_tokens(json.dumps(digest)) > ceiling:
                    digest = compact_digest(digest)
            elif _has_report_header(text, kind):
                # The model concluded with the required header itself — slice off
                # any leading monologue and keep the conclusion.
                note_src = extract_report(text, kind)
            elif text:
                # The common champion case: the final message is a long file-by-file
                # analysis that never reaches a structured conclusion. The findings
                # ARE in that prose; _clean_note recovers them (transcription pass →
                # heuristic). This is load-bearing: the model won't self-package.
                note_src = text
            if note_src is not None:
                # Always store a CLEAN note (as-is if already clean, else a
                # transcription pass, else heuristic finding lines) so the final
                # report can be assembled without any rambly text.
                clean, note_source = _clean_note(note_src, kind, pass_fn=_pass,
                                                 role=chunk_role, log=_emit)
                if clean:
                    digest["markdown_notes"].append(
                        {"chunk": c.index, "label": c.label, "md": clean,
                         "source": note_source})
                    contribution["note"] = {"label": c.label, "md": clean,
                                            "source": note_source}
                    _emit(f"chunk {c.index + 1}: findings recorded")
                else:
                    note_src = None  # nothing salvageable → fall through to unparsed
            if note_src is None and not parsed:
                # Empty/unsalvageable output — never silently drop a chunk; record
                # it so the report can flag the coverage gap.
                label = f"chunk {c.index + 1} ({c.label}): {', '.join(c.files[:4])}" \
                    + (" …" if len(c.files) > 4 else "")
                digest["unparsed_chunks"].append(label)
                contribution["unparsed"] = label
                _emit(f"chunk {c.index + 1} produced no usable findings "
                      f"(empty/truncated) — flagged as unanalyzed")
            if chunks_override is None and _cacheable(contribution,
                                                      timings[n_timed:]):
                save_chunk_note(
                    target, kind, c, head=head,
                    file_shas={r: current_shas.get(r, "") for r in c.files},
                    contribution=contribution,
                    wall_s=(timings[-1].wall_s if timings else 0.0))
            if work_dir is not None:
                (work_dir / "xref.json").write_text(json.dumps(digest, indent=2))
    except (ChatCancelled, KeyboardInterrupt):
        if work_dir is not None:
            (work_dir / "xref.json").write_text(json.dumps(digest, indent=2))
            console.print(f"[yellow]· cancelled — partial notes saved to "
                          f"{work_dir}[/]")
        else:
            console.print("[yellow]· cancelled.[/]")
        return "", None

    # Final dedupe/merge before synthesis (also re-runs the merge globally).
    digest = compact_digest(digest, ceiling_tokens=0)

    # --- Stage 3: synthesis at the EXPANDED window (one pass over all notes) -
    notes_tokens = estimate_tokens(json.dumps(digest))
    if notes_tokens > _SYNTH_REDUCE_FRAC * synth_ctx_win:
        _emit(f"aggregate notes large ({notes_tokens} tok) — 2-level reduce")
        digest = _reduce_findings(digest, eff_ctx=synth_ctx_win, pass_fn=_pass,
                                  log=_emit, role=synth_role)

    synth_ctx = (f"{health_block}\n\n<survey_notes>\n{survey_notes}\n</survey_notes>"
                 f"\n\n{_notes_block(digest)}")
    if prior_report:
        synth_ctx += f"\n\n{_prior_findings_block(prior_report)}"
    synth_goal = ("Write the final consolidated report for the repository in the "
                  "current working directory.\n\n" + _SYNTH_HINTS[kind])
    try:
        res = _pass(synth_goal, synth_ctx, "synthesis", role=synth_role)
    except (ChatCancelled, KeyboardInterrupt):
        console.print("[yellow]· cancelled.[/]")
        return "", None

    synth_text = (getattr(res, "final_text", "") or "").strip()
    if kind == "gitchange":
        # gitchange emits a structured JSON plan, not a markdown report. Robustness
        # ladder: parse the synthesis JSON → on a prose synthesis, a transcription
        # recovery pass (its own draft → JSON) → finally the aggregated per-chunk
        # digest steps (Python packaging never rambles, so a valid plan is always
        # produced). Then save plan.json + render the markdown deterministically.
        from luxe.gitkit import plan as plan_mod
        from luxe.gitkit.runner import _TITLES

        def _extract_plan_json(draft: str) -> str:
            return _plan_extract_pass(draft, pass_fn=_pass, role=synth_role)

        report, _ = plan_mod.finalize_and_save(
            target, head, synth_text, extract_fn=_extract_plan_json,
            fallback_steps=digest.get("steps"), title=_TITLES["gitchange"],
            save=save)
    else:
        report = extract_report(synth_text, kind)
        # Use the LLM synthesis ONLY if it came back clean. The champion narrates its
        # consolidation into the report, so when it doesn't: try a strict transcription
        # pass, and if THAT is still rambly, assemble the report DETERMINISTICALLY from
        # the (already-cleaned) per-chunk notes — Python packaging never rambles, so a
        # clean, complete report is guaranteed.
        if not report or _looks_rambly(report):
            cleaned = (_format_final_report(synth_text, kind, pass_fn=_pass,
                                            role=synth_role)
                       if _looks_rambly(synth_text) else report)
            if cleaned and not _looks_rambly(cleaned):
                _emit("synthesis verbose — formatted a clean report")
                report = cleaned
            else:
                _emit("synthesis unclean — assembling report deterministically")
                report = _render_report(digest, kind)
        report = report or _render_report(digest, kind) or "(no report produced)"

    if postprocess is not None:
        report = postprocess(report)

    # --- timing telemetry: raw per-pass records + cheap aggregates (B3/B4) ----
    total_wall_s = round(sum(t.wall_s for t in timings), 3)
    n_passes = len(timings)
    avg_pass_s = round(total_wall_s / n_passes, 3) if n_passes else 0.0
    if work_dir is not None:
        # The `passes` list is the raw, append-only record (ages best); the
        # aggregates are convenience derivations of it.
        (work_dir / "timing.json").write_text(json.dumps({
            "kind": kind, "head": head, "n_passes": n_passes,
            "total_wall_s": total_wall_s, "avg_pass_s": avg_pass_s,
            "passes": [t.to_dict() for t in timings],
        }, indent=2))

    from luxe.gitkit.output import finish_report
    after = (f"[dim]· survey/chunk notes: {work_dir}[/]",) if work_dir else ()
    saved = finish_report(
        console, target=target, kind=kind, report=report, head=head,
        meta={"model": backend.model, "head": head, "repo": target,
              "mode": "deep", "chunks": len(chunks),
              "total_wall_s": total_wall_s, "n_passes": n_passes,
              "avg_pass_s": avg_pass_s, **(extra_meta or {})},
        save=save, mirror=mirror, verbose=verbose, min_severity=min_severity,
        stats_line=(f"[dim]· deep · {len(chunks)} chunks · {n_passes} passes · "
                    f"{total_wall_s:.1f}s[/]"),
        after_saved=after)
    return report, saved
