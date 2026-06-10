"""gitkit runner — one read-only `run_single` pass per report.

Single-pass by contract (gitkit.sdd): no repair loop, no follow-up runs. The
runner owns the full target lifecycle: it resolves the target repo (prompting
to clone a URL when the path is NOT a git working tree), sets `repo_root`,
builds the BM25/symbol indices for the target, runs one read-only pass, then
restores whatever repo_root/indices were resident before (so a `luxe chat`
session is left untouched after a `/gitsummary` over a freshly-cloned repo).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from luxe.agents import prompts

# Per-run generation ceiling for the FINAL report (safety margin, not the fix —
# WS1's prompt discipline keeps reports well under this). The first test hit the
# chat role's 8192 cap and truncated mid-report. Applied via a per-run
# model_copy in run_git_report; never mutates the shared role / chat.yaml.
GITKIT_MAX_TOKENS = 16384

# On-screen preview cap (full report is always saved + available via --verbose).
_PREVIEW_LINES = 30

_H1_RE = re.compile(r"^#\s", re.MULTILINE)


def extract_report(text: str, kind: str | None = None) -> str:
    """Slice the report from its header onward, dropping any leading monologue the
    model emits before it (WS1 safety net for the "treats the final turn as more
    reasoning" failure mode).

    When `kind` is given, rank ALL `# ` headers by case-insensitive word overlap
    with the kind's required title (`_TITLES[kind]`) and slice from the
    best-scoring one (tie → earliest) — an exact title scores highest, but a
    near-title ("# Repository audit — preliminary") still beats a monologue
    heading. All headers scoring zero → fall back to the first `# ` header, then
    to the unchanged text (never drop content)."""
    text = text or ""
    if kind and kind in _TITLES:
        want = {w.lower() for w in re.findall(r"[a-z]+", _TITLES[kind], re.I)}
        best_pos, best_score = -1, 0
        for m in re.finditer(r"^#\s+(.+)$", text, re.MULTILINE):
            words = {w.lower() for w in re.findall(r"[a-z]+", m.group(1), re.I)}
            score = len(want & words)
            if score > best_score:        # strict > keeps the EARLIEST on ties
                best_score, best_pos = score, m.start()
        if best_pos != -1:
            return text[best_pos:]
    m = _H1_RE.search(text)
    return text[m.start():] if m else text

# kind -> (task_type overlay reused, goal ask, directive HINT). No new task
# types: each maps onto an existing overlay; the per-kind directive rides in the
# goal (gitkit.sdd / agents.sdd single-source rule).
KINDS: dict[str, tuple[str, str, str]] = {
    "gitaudit": (
        "review",
        "Audit the codebase in the current working directory: orient, find serious "
        "bugs & security issues, and identify the highest-leverage structural "
        "improvements — in one report.",
        prompts.GIT_AUDIT_HINT,
    ),
    "gitchange": (
        "review",
        "Analyze the codebase in the current working directory and produce an "
        "APPLY-READY, ordered structural change plan.",
        prompts.GIT_CHANGE_HINT,
    ),
    # Internal kind only (never a CLI command): `gitaudit --base/--pr` switches
    # to it after resolving the diff scope. Reuses the existing `review`
    # task_type (gitkit.sdd: no new task types).
    "gitaudit-diff": (
        "review",
        "Audit ONLY the change between the given base and HEAD of the "
        "repository in the current working directory.",
        prompts.GIT_AUDIT_DIFF_HINT,
    ),
}

_TITLES = {
    "gitaudit": "Repository audit",
    "gitchange": "Change plan",
    "gitaudit-diff": "Diff audit",
}

# Kinds that consume a prior same-commit gitaudit's findings as context.
_PRIOR_FINDINGS_KINDS = ("gitchange",)
# Kinds that emit a structured apply-ready plan (parsed + saved as plan.json).
_PLAN_KINDS = ("gitchange",)


def _looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "git@", "ssh://"))


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
    args = ["--filter=blob:none"] if full_history else ["--depth=1"]
    console.print(f"[dim]· cloning {url} → {dest}…[/]")
    proc = subprocess.run(
        ["git", "clone", *args, url, str(dest)], capture_output=True, text=True)
    if proc.returncode != 0:
        console.print(f"[red]clone failed:[/] {(proc.stderr or proc.stdout).strip()}")
        return False
    return True


def _resolve_or_clone(path, *, full_history: bool, console, reader) -> str | None:
    """Return a local git-repo path for `path`. If `path` is not a git working
    tree, prompt the user for a URL and clone a local copy (showing the path);
    returns None if the user cancels or the clone fails."""
    from luxe.gitkit import health

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


def _activity_callbacks(update, cancel=None):
    """Build (on_tool_event, on_token) that coalesce tool calls into a phased
    status string passed to `update(text)`: `analyzing… read_file (31) · grep
    (12)` while tools fire, then `writing report…` once the model starts emitting
    prose after the last tool. When `cancel` is provided (interactive TUI), each
    callback raises `ChatCancelled` if cancellation was requested. Factored out so
    the coalescing/phasing is unit-testable without a TTY."""
    from collections import Counter

    from luxe.chat.render import raise_if_cancelled

    counts: Counter = Counter()
    state = {"writing": False}

    def _text() -> str:
        if not counts:
            return "analyzing…"
        top = " · ".join(f"{n} ({c})" for n, c in counts.most_common(4))
        return f"analyzing… {top}"

    def on_event(tc):
        counts[getattr(tc, "name", "?")] += 1
        state["writing"] = False  # more tools → back to analyzing
        update(_text())
        if cancel is not None:
            raise_if_cancelled(cancel)

    def on_token(_delta):
        if cancel is not None:
            raise_if_cancelled(cancel)
        # First prose after at least one tool call = the report being written.
        if counts and not state["writing"]:
            state["writing"] = True
            update("writing report…")

    return on_event, on_token


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
    from rich.markdown import Markdown

    from luxe import search as search_mod
    from luxe import symbols as symbols_mod
    from luxe.agents.single import run_single
    from luxe.backend import Backend
    from luxe.chat.render import truncate_for_display
    from luxe.cli import _detect_languages_for_repo
    from luxe.gitkit import health, store
    from luxe.mcp.server import make_read_only_role
    from luxe.tools.fs import get_repo_root, set_repo_root

    if kind not in KINDS:
        raise ValueError(f"unknown gitkit kind {kind!r}; expected {sorted(KINDS)}")
    if base is not None and pr is not None:
        console.print("[red]· --base and --pr are mutually exclusive.[/]")
        return "", None
    if (base is not None or pr is not None) and kind != "gitaudit":
        console.print("[red]· --base/--pr apply to gitaudit only.[/]")
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
        from luxe.gitkit import diffscope
        pr_base, why = diffscope.pr_base_ref(target, pr)
        if pr_base is None:
            console.print(f"[red]· {why}[/]")
            return "", None
        console.print(f"[dim]· PR #{pr} base branch: {pr_base}[/]")
        base = pr_base

    # Index/repo-root lifecycle: reuse resident indices when they already cover
    # the target (the common REPL case); otherwise build for the target and
    # restore the prior global state afterward (so chat is left untouched).
    prev_root = get_repo_root()
    prev_bm25 = search_mod._index   # module-level resident index (no public getter)
    prev_sym = symbols_mod._index
    reuse = prev_root is not None and str(prev_root) == target
    swapped = False

    try:
        if reuse and expected_head:
            cur = health.current_head(target)
            if cur and cur != expected_head:
                console.print(
                    f"[yellow]· repo HEAD moved since indices were built "
                    f"({expected_head} → {cur}); search results may be stale.[/]")
        if not reuse:
            set_repo_root(target)
            console.print("[dim]· Indexing repository for search…[/]")
            search_mod.set_index(search_mod.build_bm25_index(target))
            symbols_mod.set_index(symbols_mod.build_symbol_index(target))
            swapped = True

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
        from luxe.gitkit import deep as deep_mod
        from luxe.repo_index import build_repo_summary
        sym_index = symbols_mod._index
        summary = build_repo_summary(
            target, symbol_coverage=getattr(sym_index, "coverage", None))
        # gitrefactor consumes a prior same-commit gitreview's FINDINGS (not the
        # whole report — extract_findings keeps the payload small) so refactor steps
        # don't undo security fixes. Consume-if-present; empty when no review exists.
        prior_findings = ""
        if kind in _PRIOR_FINDINGS_KINDS:
            prior_md = store.latest_report_for(
                target, "gitaudit", health.current_head(target))
            prior_findings = store.extract_findings(prior_md or "")
            if prior_findings:
                console.print("[dim]· using prior gitaudit findings to inform the "
                              "change plan[/]")

        # --- DIFF AUDIT (`--base`/`--pr`): scope everything to the change ----
        diff_extra = ""
        diff_post = None
        extra_meta: dict = {}
        if base is not None:
            from luxe.gitkit import diffscope
            kind = "gitaudit-diff"
            task_type, ask, hint = KINDS[kind]
            goal = f"{ask}\n\n{hint}"
            base_ref = diffscope.resolve_base_ref(target, base)
            if base_ref is None:
                console.print(f"[red]· cannot resolve base ref {base!r} "
                              "(tried as-is and origin/<ref>).[/]")
                return "", None
            mb = diffscope.merge_base(target, base_ref)
            if mb is None:
                console.print(f"[red]· no merge-base between {base_ref} and HEAD.[/]")
                return "", None
            changed = diffscope.changed_files(target, mb)
            if not changed:
                console.print(f"[yellow]· no changes vs {base_ref} "
                              f"(merge-base {mb[:8]}) — nothing to audit.[/]")
                return "", None
            recs = diffscope.file_recs(target, changed)
            stats = diffscope.diff_stats(target, mb)
            hunks = diffscope.changed_hunks(target, mb)
            eff_ctx = deep_mod.base_ctx(role_cfg)
            diff_cap = max(256, int(eff_ctx * diffscope.DIFF_BUDGET_FRAC))
            full_diff_block = diffscope.change_diff_block(
                target, mb, base_label=base_ref, max_tokens=diff_cap)
            console.print(f"[dim]· diff scope: {base_ref} (merge-base {mb[:8]}) "
                          f"— {stats[0]} files, +{stats[1]}/−{stats[2]}[/]")
            extra_meta = {"base": base_ref, "merge_base": mb[:12]}

            def diff_post(rep: str) -> str:
                # deterministic prior + fixed caveat — never model-trusted
                rep = diffscope.apply_tag_priors(rep, hunks)
                return diffscope.ensure_header(rep, base_ref, mb, stats)

            # Footprint-gate on the CHANGED files only (most personal branches
            # land single-pass); --deep/--no-deep still override.
            changed_tokens = sum(r.tokens for r in recs)
            use_deep = deep if deep is not None else (
                changed_tokens >= deep_mod._DEEP_TRIGGER_FRAC * eff_ctx)
            if use_deep:
                content_budget = max(1, int(deep_mod.deep_window(role_cfg)
                                            * deep_mod._CONTENT_BUDGET_FRAC))
                chunks = deep_mod.build_chunks(
                    recs, content_budget=content_budget, symbol_index=sym_index)
                chunk_blocks = {
                    c.index: diffscope.change_diff_block(
                        target, mb, base_label=base_ref,
                        max_tokens=diff_cap, files=c.files)
                    for c in chunks if c.files}
                # Opportunistic: inject a FRESH whole-repo map's survey notes;
                # NEVER generate one, and diff runs never write map/.
                survey_inject = None
                st = deep_mod.map_status(target, head=health.current_head(target))
                if st.state is deep_mod.MapState.FRESH:
                    sn = deep_mod._map_dir(target) / "survey_notes.md"
                    if sn.is_file():
                        survey_inject = sn.read_text()
                        console.print("[dim]· injecting fresh whole-repo survey "
                                      "notes into the diff audit[/]")
                return deep_mod.run_deep_report(
                    kind, target=target, task_type=task_type, backend=backend,
                    role_cfg=role_cfg, languages=languages, console=console,
                    reader=reader, summary=summary, symbol_index=sym_index,
                    health_block=(health.gather_context(target)
                                  + "\n\n" + full_diff_block),
                    save=save, verbose=verbose, cancel=cancel,
                    max_chunks=max_chunks, mirror=mirror,
                    chunks_override=chunks, chunk_extra_blocks=chunk_blocks,
                    survey_notes_override=survey_inject,
                    postprocess=diff_post, extra_meta=extra_meta)
            # single-pass diff: the capped full diff + the changed-file list in
            # the existing <chunk_files> shape ride along as pure data.
            one = deep_mod.build_chunks(recs, content_budget=10 ** 9,
                                        symbol_index=sym_index)
            diff_extra = f"{full_diff_block}\n\n{deep_mod._chunk_block(one[0], 1)}"
        else:
            # Both kinds auto-select single-pass vs deep by footprint: small/medium
            # repos stay single-pass (one window holds the whole analysis), large
            # repos take the staged deep map-reduce (the 46-repo sweep proved
            # single-pass EMPTIES on large repos — the model explores and
            # under-concludes; deep's synthesis stage re-consolidates per-chunk
            # notes into one report/plan). `--deep/--no-deep` overrides;
            # `--max-chunks` caps. Neither path runs an in-agent repair loop.
            use_deep = deep_mod.should_use_deep(summary, role_cfg, override=deep)
            if use_deep:
                return deep_mod.run_deep_report(
                    kind, target=target, task_type=task_type, backend=backend,
                    role_cfg=role_cfg, languages=languages, console=console,
                    reader=reader, summary=summary, symbol_index=sym_index,
                    health_block=health.gather_context(target), save=save,
                    verbose=verbose, cancel=cancel, max_chunks=max_chunks,
                    rebuild_map=rebuild_map, prior_report=prior_findings,
                    mirror=mirror,
                )

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
        from luxe.chat.render import ChatCancelled
        try:
            if console.is_terminal:
                with console.status("[dim]gathering context & loading model…[/]",
                                    spinner="dots") as status:
                    on_event, on_token = _activity_callbacks(
                        lambda t: status.update(f"[dim]{t}[/]"), cancel=cancel)
                    result = _do_run(on_event, on_token)
            else:
                console.print("[dim]· gathering git history + GitHub metadata…[/]")
                on_event, on_token = _activity_callbacks(lambda t: None, cancel=cancel)
                result = _do_run(on_event, on_token)
        except (ChatCancelled, KeyboardInterrupt):
            console.print("[yellow]· cancelled.[/]")
            return "", None

        _head = health.current_head(target)
        if kind in _PLAN_KINDS:
            # gitchange emits a structured plan. The champion rarely emits clean JSON
            # agentically, so on a parse miss we run a low-judgment transcription pass
            # (its own prose draft → JSON), then render the markdown deterministically.
            from luxe.gitkit import plan as plan_mod

            def _extract_plan_json(draft: str) -> str:
                r = run_single(
                    backend, role_cfg,
                    goal="Convert the change plan draft into the required JSON.\n\n"
                         + prompts.GIT_CHANGE_EXTRACT_HINT,
                    task_type=task_type, languages=languages,
                    extra_context=f"<plan_draft>\n{draft}\n</plan_draft>",
                    phase="chat", run_id="gitkit-gitchange-extract")
                return (getattr(r, "final_text", "") or "")

            report, _ = plan_mod.finalize_and_save(
                target, _head, (result.final_text or "").strip(),
                extract_fn=_extract_plan_json, title=_TITLES[kind])
        else:
            # WS1.2: slice off any leading monologue before the report's required title.
            report = extract_report((result.final_text or "").strip(), kind) or \
                "(no report produced)"
            if diff_post is not None:
                report = diff_post(report)

        # Full report is always saved; on screen show a preview unless verbose.
        saved: Path | None = None
        if save:
            _wall = round(result.wall_s, 3)
            saved = store.save_report(
                target, kind, report,
                meta={"model": model, "head": _head, "repo": target,
                      "total_wall_s": _wall, "avg_pass_s": _wall, "n_passes": 1,
                      **extra_meta},
            )
            if mirror and store.mirror_to_repo(target, kind, report, _head):
                console.print("[dim]· mirrored report to <repo>/.luxe/gitkit/[/]")

        console.print()
        if verbose:
            console.print(Markdown(report))
        else:
            shown, hidden = truncate_for_display(report, max_lines=_PREVIEW_LINES)
            console.print(Markdown(shown))
            if hidden:
                console.print(f"[dim]… +{hidden} more lines — full report below[/]")
        console.print(
            f"\n[dim]· {result.steps} steps · {result.tool_calls_total} tool calls "
            f"· {result.wall_s:.1f}s · {result.completion_tokens} out-tok[/]")
        if saved:
            tail = "" if verbose else " — re-run with --verbose / -v for the full report"
            console.print(f"[green]✓[/] report saved to [cyan]{saved}[/]{tail}")
        return report, saved
    finally:
        if swapped:
            if prev_root is not None:
                set_repo_root(prev_root)
            if prev_bm25 is not None:
                search_mod.set_index(prev_bm25)
            else:
                search_mod.reset_index()
            if prev_sym is not None:
                symbols_mod.set_index(prev_sym)
            else:
                symbols_mod.reset_index()
