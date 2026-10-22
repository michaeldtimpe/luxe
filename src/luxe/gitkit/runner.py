"""gitkit runner — one read-only `run_single` pass per report.

Single-pass by contract (gitkit.sdd): no repair loop, no follow-up runs. The
runner owns the full target lifecycle: it resolves the target repo (prompting
to clone a URL when the path is NOT a git working tree), sets `repo_root`,
builds the BM25/symbol indices for the target, runs one read-only pass, then
restores whatever repo_root/indices were resident before (so a `luxe chat`
session is left untouched after a `/gitaudit` over a freshly-cloned repo).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

from luxe import gitclone
from luxe.cancel import ChatCancelled
from luxe.gitkit import deep as deep_mod
from luxe.gitkit import diffscope, health, store
from luxe.gitkit import plan as plan_mod
from luxe.gitkit.deep.salvage import _plan_extract_pass
from luxe.gitkit.kinds import (  # re-exported: tests + scripts import these from here
    GITKIT_MAX_TOKENS as GITKIT_MAX_TOKENS,
    KINDS as KINDS,
    _TITLES as _TITLES,
    _activity_callbacks as _activity_callbacks,
    extract_report as extract_report,
    run_with_activity,
)
from luxe.gitkit.output import finish_report
from luxe.gitkit.workspace import indexed_target
from luxe.repo_index import _detect_languages_for_repo, build_repo_summary
from luxe.roles import make_read_only_role

# Kinds that consume a prior same-commit gitaudit's findings as context.
_PRIOR_FINDINGS_KINDS = ("gitchange",)
# Kinds that emit a structured apply-ready plan (parsed + saved as plan.json).
_PLAN_KINDS = ("gitchange",)


def _derive_dest(base_path: str | Path, url: str) -> Path:
    """Local destination for a clone: `<base dir>/<repo name>`, de-duplicated."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    name = name or "repo"
    base = Path(base_path)
    parent = base if base.is_dir() else base.parent
    dest = parent / name
    i = 2
    while dest.exists():
        dest = parent / f"{name}-{i}"
        i += 1
    return dest


def _clone(url: str, dest: Path, *, full_history: bool, console) -> bool:
    """Clone `url` into `dest`. Full history for summaries; shallow otherwise."""
    console.print(f"[dim]· cloning {url} → {dest}…[/]")
    ok, err = gitclone.clone(url, dest, full_history=full_history)
    if not ok:
        console.print(f"[red]clone failed:[/] {err}")
        return False
    return True


def _resolve_or_clone(path, *, full_history: bool, console, reader) -> str | None:
    """Return a local git-repo path for `path`. If `path` is not a git working
    tree, prompt the user for a URL and clone a local copy (showing the path);
    returns None if the user cancels or the clone fails."""
    if health.is_git_repo(path):
        return str(Path(path).resolve())

    console.print(f"[yellow]· {Path(path).resolve()} is not a git repository.[/]")
    url = reader("  git URL to clone (blank to cancel): ").strip()
    if not url:
        return None
    dest = _derive_dest(path, url)
    confirm = reader(f"  clone into {dest}? [Y/n]: ").strip().lower()
    if confirm in ("n", "no"):
        return None
    if not _clone(url, dest, full_history=full_history, console=console):
        return None
    return str(dest.resolve())


@dataclass
class DiffScope:
    """A resolved `gitaudit --base/--pr` change scope: the merge-base, the
    changed files' recs, and the `-U10` diff (fetched ONCE; every block is a
    slice of `per_file`). Deterministic data only — no model calls."""
    base_ref: str
    merge_base: str
    recs: list
    stats: tuple
    hunks: dict
    eff_ctx: int
    diff_cap: int
    per_file: dict
    full_diff_block: str

    @property
    def extra_meta(self) -> dict:
        return {"base": self.base_ref, "merge_base": self.merge_base[:12]}

    def postprocess(self, rep: str) -> str:
        # deterministic prior + fixed caveat — never model-trusted
        rep = diffscope.apply_tag_priors(rep, self.hunks)
        return diffscope.ensure_header(rep, self.base_ref, self.merge_base,
                                       self.stats)

    def wants_deep(self) -> bool:
        """Footprint gate over the CHANGED files only."""
        changed_tokens = sum(r.tokens for r in self.recs)
        return changed_tokens >= deep_mod._DEEP_TRIGGER_FRAC * self.eff_ctx

    def single_pass_block(self, symbol_index) -> str:
        """The capped full diff + the changed-file list in the existing
        `<chunk_files>` shape, for the single-pass diff audit."""
        one = deep_mod.build_chunks(self.recs, content_budget=10 ** 9,
                                    symbol_index=symbol_index)
        return f"{self.full_diff_block}\n\n{deep_mod._chunk_block(one[0], 1)}"

    def deep_kwargs(self, target: str, *, role_cfg, symbol_index,
                    console) -> dict:
        """The diff-specific `run_deep_report` arguments: chunks over the
        changed files, a chunk-scoped `<change_diff>` per chunk, and —
        opportunistically — a FRESH whole-repo map's survey notes (never
        generated; diff runs never write map/)."""
        content_budget = max(1, int(deep_mod.deep_window(role_cfg)
                                    * deep_mod._CONTENT_BUDGET_FRAC))
        chunks = deep_mod.build_chunks(
            self.recs, content_budget=content_budget, symbol_index=symbol_index)
        chunk_blocks = {
            c.index: diffscope.change_diff_block(
                target, self.merge_base, base_label=self.base_ref,
                max_tokens=self.diff_cap, files=c.files,
                stats=self.stats, per_file=self.per_file)
            for c in chunks if c.files}
        survey_inject = deep_mod.fresh_survey_notes(
            target, head=health.current_head(target))
        if survey_inject is not None:
            console.print("[dim]· injecting fresh whole-repo survey "
                          "notes into the diff audit[/]")
        return dict(
            health_block=(health.gather_context(target)
                          + "\n\n" + self.full_diff_block),
            chunks_override=chunks, chunk_extra_blocks=chunk_blocks,
            survey_notes_override=survey_inject,
            postprocess=self.postprocess, extra_meta=self.extra_meta)


def _prepare_diff_scope(target: str, base: str, *, role_cfg,
                        console) -> DiffScope | None:
    """Resolve `base` → merge-base → changed files → the capped diff blocks.
    None (after printing why) when the ref won't resolve, there is no
    merge-base, or nothing changed."""
    base_ref = diffscope.resolve_base_ref(target, base)
    if base_ref is None:
        console.print(f"[red]· cannot resolve base ref {base!r} "
                      "(tried as-is and origin/<ref>).[/]")
        return None
    mb = diffscope.merge_base(target, base_ref)
    if mb is None:
        console.print(f"[red]· no merge-base between {base_ref} and HEAD.[/]")
        return None
    changed = diffscope.changed_files(target, mb)
    if not changed:
        console.print(f"[yellow]· no changes vs {base_ref} "
                      f"(merge-base {mb[:8]}) — nothing to audit.[/]")
        return None
    recs = diffscope.file_recs(target, changed)
    stats = diffscope.diff_stats(target, mb)
    hunks = diffscope.changed_hunks(target, mb)
    eff_ctx = deep_mod.base_ctx(role_cfg)
    diff_cap = max(256, int(eff_ctx * diffscope.DIFF_BUDGET_FRAC))
    # the -U10 diff is fetched ONCE; every block below is a slice of it
    per_file = diffscope.file_diffs(target, mb)
    full_diff_block = diffscope.change_diff_block(
        target, mb, base_label=base_ref, max_tokens=diff_cap,
        stats=stats, per_file=per_file)
    console.print(f"[dim]· diff scope: {base_ref} (merge-base {mb[:8]}) "
                  f"— {stats[0]} files, +{stats[1]}/−{stats[2]}[/]")
    return DiffScope(base_ref=base_ref, merge_base=mb, recs=recs, stats=stats,
                     hunks=hunks, eff_ctx=eff_ctx, diff_cap=diff_cap,
                     per_file=per_file, full_diff_block=full_diff_block)


def _check_kind_args(kind: str, base: str | None, pr: int | None,
                     console) -> bool:
    """Validate the kind and the `--base`/`--pr` combination (printing why
    not). An unknown kind is a programming error and raises."""
    if kind not in KINDS:
        raise ValueError(f"unknown gitkit kind {kind!r}; expected {sorted(KINDS)}")
    if base is not None and pr is not None:
        console.print("[red]· --base and --pr are mutually exclusive.[/]")
        return False
    if (base is not None or pr is not None) and kind != "gitaudit":
        console.print("[red]· --base/--pr apply to gitaudit only.[/]")
        return False
    return True


def _pr_base(target: str, pr: int, console) -> str | None:
    """The PR's base branch via gh, or None after printing the failure."""
    pr_base, why = diffscope.pr_base_ref(target, pr)
    if pr_base is None:
        console.print(f"[red]· {why}[/]")
        return None
    console.print(f"[dim]· PR #{pr} base branch: {pr_base}[/]")
    return pr_base


def _warn_if_head_moved(target: str, expected_head: str | None,
                        console) -> None:
    """When the resident indices already cover `target` (the REPL case) and
    HEAD has moved since they were built, say the search may be stale."""
    from luxe.tools.fs import get_repo_root
    prev_root = get_repo_root()
    reuse = prev_root is not None and str(prev_root) == target
    if reuse and expected_head:
        cur = health.current_head(target)
        if cur and cur != expected_head:
            console.print(
                f"[yellow]· repo HEAD moved since indices were built "
                f"({expected_head} → {cur}); search results may be stale.[/]")


def _prior_findings(kind: str, target: str, console) -> str:
    """gitchange consumes a prior same-commit gitaudit's FINDINGS (not the
    whole report — extract_findings keeps the payload small) so change steps
    don't undo security fixes. Consume-if-present; empty when no audit
    exists."""
    if kind not in _PRIOR_FINDINGS_KINDS:
        return ""
    prior_md = store.latest_report_for(
        target, "gitaudit", health.current_head(target))
    prior_findings = store.extract_findings(prior_md or "")
    if prior_findings:
        console.print("[dim]· using prior gitaudit findings to inform the "
                      "change plan[/]")
    return prior_findings


def _single_extract_pass(run_single, backend, task_type, languages,
                         goal: str, ctx: str, _label: str, role):
    """The single-pass path's `pass_fn` for `_plan_extract_pass`: one
    tool-callback-free `run_single` under its own run_id."""
    return run_single(
        backend, role, goal=goal, task_type=task_type,
        languages=languages, extra_context=ctx,
        phase="chat", run_id="gitkit-gitchange-extract")


def _package_report(kind: str, text: str, *, target: str, head: str,
                    scope: DiffScope | None, save: bool, pass_fn,
                    role) -> str:
    """Turn the single pass's final text into the saved report."""
    if kind in _PLAN_KINDS:
        # gitchange emits a structured plan. The champion rarely emits clean JSON
        # agentically, so on a parse miss we run a low-judgment transcription pass
        # (its own prose draft → JSON), then render the markdown deterministically.
        report, _ = plan_mod.finalize_and_save(
            target, head, text,
            extract_fn=lambda draft: _plan_extract_pass(
                draft, pass_fn=pass_fn, role=role),
            title=_TITLES[kind], save=save)
        return report
    # WS1.2: slice off any leading monologue before the report's required title.
    report = extract_report(text, kind) or "(no report produced)"
    return scope.postprocess(report) if scope is not None else report


def run_git_report(
    kind: str,
    *,
    cfg,
    repo_path: str | Path,
    console,
    reader=None,
    save: bool = True,
    verbose: bool = False,
    expected_head: str | None = None,
    cancel=None,
    deep: bool | None = None,
    max_chunks: int | None = None,
    rebuild_map: bool = False,
    mirror: bool = True,
    base: str | None = None,
    pr: int | None = None,
    min_severity: str | None = None,
    no_incremental: bool = False,
) -> tuple[str, Path | None]:
    """Run a read-only analysis over a repo and report the result.

    `base`/`pr` (gitaudit only, mutually exclusive) switch to the DIFF AUDIT:
    only the change between the base ref (or the PR's base, resolved via gh)
    and HEAD is audited, under the internal `gitaudit-diff` kind. Routing
    still footprint-gates on the CHANGED files only; diff runs never write
    the per-repo `map/` cache.

    Small/medium repos take the single-pass path (one `run_single`, unchanged).
    Large repos (estimated token footprint over the deep threshold, or `deep=True`)
    take the staged DEEP path (`deep.run_deep_report`): survey → chunk → per-chunk
    notes → synthesis, with a persistent per-repo `map/` cache.

    Args:
        kind: gitaudit | gitchange.
        cfg: loaded PipelineConfig (provides oMLX URL, model, role).
        repo_path: target path; if it is not a git working tree, the user is
            prompted to clone a URL into a local copy.
        console: Rich console for output.
        reader: prompt callable (defaults to `console.input`) — injectable for
            tests / non-interactive callers.
        save: when True, persist the FULL markdown report under ~/.luxe/reports/.
        verbose: print the full report on screen; otherwise a truncated preview.
        expected_head: if set AND the resident indices already cover the target,
            warn when the repo's HEAD has moved (indices may be stale).
        deep: None = auto-select by footprint; True/False force deep / single-pass.
        max_chunks: deep-mode safety valve — cap chunks analyzed (loud skip log).
        rebuild_map: deep-mode — ignore the cached `map/` and re-survey/re-chunk.

    Returns:
        (report_text, saved_path | None); ("", None) if the user cancels.

    Side effects: one or more `run_single` calls (read-only role, per-run token
    headroom copy); BM25/symbol indices and repo_root are swapped to the target
    and restored afterward; report + (deep) map/notes file writes; optional clone.
    """
    from luxe import symbols as symbols_mod
    from luxe.agents.single import run_single
    from luxe.backend import Backend

    if not _check_kind_args(kind, base, pr, console):
        return "", None
    task_type, ask, hint = KINDS[kind]
    reader = reader or console.input

    target = _resolve_or_clone(
        repo_path, full_history=(kind == "gitaudit"),
        console=console, reader=reader)
    if target is None:
        console.print("[yellow]· cancelled.[/]")
        return "", None

    if pr is not None:
        base = _pr_base(target, pr, console)
        if base is None:
            return "", None

    # Index/repo-root lifecycle: reuse resident indices when they already cover
    # the target (the common REPL case); otherwise build for the target and
    # restore the prior global state afterward (so chat is left untouched).
    _warn_if_head_moved(target, expected_head, console)
    with indexed_target(target, console=console):
        languages = _detect_languages_for_repo(target)
        model = cfg.model_for_slot("chat")
        console.print(f"[dim]· {_TITLES[kind]} — model: {model} (read-only)[/]")

        backend = Backend(base_url=cfg.omlx_base_url, model=model)
        # WS1.3: per-run token headroom so the final report can't truncate
        # mid-thought. A copy — never mutates the shared role / chat.yaml.
        role_cfg = make_read_only_role(cfg.role("monolith")).model_copy(
            update={"max_tokens_per_turn": GITKIT_MAX_TOKENS})
        goal = f"{ask}\n\n{hint}"

        # Single-pass (default for small/medium) vs staged deep mode (large repos
        # or --deep). The footprint decision reuses the just-built symbol index.
        sym_index = symbols_mod._index
        summary = build_repo_summary(
            target, symbol_coverage=getattr(sym_index, "coverage", None))
        prior_findings = _prior_findings(kind, target, console)

        # --- DIFF AUDIT (`--base`/`--pr`): scope everything to the change ----
        scope: DiffScope | None = None
        if base is not None:
            kind = "gitaudit-diff"
            task_type, ask, hint = KINDS[kind]
            goal = f"{ask}\n\n{hint}"
            scope = _prepare_diff_scope(target, base, role_cfg=role_cfg,
                                        console=console)
            if scope is None:
                return "", None

        # Both kinds auto-select single-pass vs deep by footprint: small/medium
        # repos stay single-pass (one window holds the whole analysis), large
        # repos take the staged deep map-reduce (the 46-repo sweep proved
        # single-pass EMPTIES on large repos — the model explores and
        # under-concludes; deep's synthesis stage re-consolidates per-chunk
        # notes into one report/plan). `--deep/--no-deep` overrides;
        # `--max-chunks` caps. Neither path runs an in-agent repair loop. A
        # diff audit footprint-gates on the CHANGED files only (most personal
        # branches land single-pass).
        if scope is not None:
            use_deep = deep if deep is not None else scope.wants_deep()
        else:
            use_deep = deep_mod.should_use_deep(summary, role_cfg, override=deep)
        if use_deep:
            deep_kw = dict(
                target=target, task_type=task_type, backend=backend,
                role_cfg=role_cfg, languages=languages, console=console,
                reader=reader, summary=summary, symbol_index=sym_index,
                save=save, verbose=verbose, cancel=cancel,
                max_chunks=max_chunks, mirror=mirror, min_severity=min_severity)
            if scope is not None:
                return deep_mod.run_deep_report(
                    kind, **deep_kw,
                    **scope.deep_kwargs(target, role_cfg=role_cfg,
                                        symbol_index=sym_index,
                                        console=console))
            return deep_mod.run_deep_report(
                kind, **deep_kw, health_block=health.gather_context(target),
                rebuild_map=rebuild_map, prior_report=prior_findings,
                no_incremental=no_incremental)

        # single-pass diff: the capped full diff + the changed-file list in
        # the existing <chunk_files> shape ride along as pure data.
        diff_extra = scope.single_pass_block(sym_index) if scope else ""

        def _do_run(on_event=None, on_token=None):
            extra_context = health.gather_context(target)
            if diff_extra:
                extra_context += f"\n\n{diff_extra}"
            if prior_findings:
                extra_context += (f"\n\n<prior_findings>\n{prior_findings}\n"
                                  "</prior_findings>")
            return run_single(
                backend, role_cfg, goal=goal, task_type=task_type,
                languages=languages, extra_context=extra_context,
                on_tool_event=on_event, on_token=on_token,
                phase="chat", run_id=f"gitkit-{kind}",
            )

        # WS2: phased spinner + coalesced tool counts while the model works
        # (terminal only; gitkit is self-contained, no chat LiveActivity). When a
        # `cancel` token is supplied (interactive TUI), esc/Ctrl-C aborts cleanly.
        try:
            result = run_with_activity(
                console, "gathering context & loading model", cancel, _do_run,
                plain_note="[dim]· gathering git history + GitHub metadata…[/]")
        except (ChatCancelled, KeyboardInterrupt):
            console.print("[yellow]· cancelled.[/]")
            return "", None

        _head = health.current_head(target)
        extract_pass = functools.partial(
            _single_extract_pass, run_single, backend, task_type, languages)
        report = _package_report(
            kind, (result.final_text or "").strip(), target=target,
            head=_head, scope=scope, save=save, pass_fn=extract_pass,
            role=role_cfg)

        _wall = round(result.wall_s, 3)
        saved = finish_report(
            console, target=target, kind=kind, report=report, head=_head,
            meta={"model": model, "head": _head, "repo": target,
                  "total_wall_s": _wall, "avg_pass_s": _wall, "n_passes": 1,
                  **(scope.extra_meta if scope else {})},
            save=save, mirror=mirror, verbose=verbose, min_severity=min_severity,
            stats_line=(
                f"\n[dim]· {result.steps} steps · {result.tool_calls_total} "
                f"tool calls · {result.wall_s:.1f}s · "
                f"{result.completion_tokens} out-tok[/]"))
        return report, saved
