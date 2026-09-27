"""Fallback-kit + host-diagnostic commands: `ready` (alias `doctor`),
`smoke`, `repair`, `outage`, `net`, `planeproxy`, `claudecode`, `unload`,
`update`.

Monkeypatch note: `smoke`/`ready` look up `_smoke_self_repair`,
`build_ready_doctor` and `_chat_cfg` in THIS module's globals — patch
`luxe.cli.kit.<name>`, not the `luxe.cli` re-export.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click

from luxe import gitcmd
from luxe import textfmt
from luxe.cli._common import _chat_cfg, _select_backend, console, main
from luxe.paths import luxe_home


@main.command(name="unload")
@click.option("--except", "except_for", multiple=True,
              help="Model ID(s) to keep resident (repeatable). Default: unload all.")
@click.option("--force", is_flag=True, default=False,
              help="Unload even when the default backend is SHARED (another "
                   "host's live models go too).")
def unload_models(except_for: tuple[str, ...], force: bool):
    """Unload all currently-loaded models from the configured endpoint to
    free RAM (the chat config's default backend — `$LUXE_CONFIG` honoured)."""
    from luxe.chat.inspection import endpoint_fixes

    cfg = _chat_cfg()
    entry = cfg.backend_entry(cfg.default_backend_name())
    if entry.is_shared() and not force:
        # B5: a shared endpoint's residents are other clients' live
        # sessions. Evicting them is never a default.
        console.print(f"[red]✗ {cfg.default_backend_name()} ({entry.base_url}) "
                      "is a shared endpoint — unloading would evict other "
                      "hosts' models. Unload on that host, or pass --force.[/]")
        sys.exit(2)
    b = entry.build_backend("(unload-cli)")
    if not b.health(timeout_s=10.0):
        console.print(f"[red]{entry.engine_label()} unreachable at "
                      f"{entry.base_url} — {endpoint_fixes(entry)['start']}[/]")
        sys.exit(2)
    loaded = b.loaded_models()
    if not loaded:
        console.print("[dim]No models currently loaded — nothing to unload.[/]")
        return
    keep = set(except_for or [])
    console.print(f"Loaded models: {len(loaded)}")
    for m in loaded:
        marker = "[dim](kept)[/]" if m in keep else ""
        console.print(f"  · {m} {marker}")
    results = b.unload_all_loaded(except_for=list(keep))
    n_ok = sum(1 for v in results.values() if v)
    console.print(f"\n[bold]Unloaded {n_ok}/{len(results)} model(s)[/]")
    if n_ok < len(results):
        for mid, ok in results.items():
            if not ok:
                console.print(f"  [yellow]✗ {mid} — unload failed[/]")


@main.command(name="update")
@click.option("--no-sync", is_flag=True, default=False,
              help="Skip `uv sync` after pulling (code only, no dep changes).")
def update_cmd(no_sync: bool):
    """Update THIS host's luxe checkout: fetch → rebase onto origin/main →
    `uv sync` with the canonical extras (chat+dev+analyzers+web). Says what's
    incoming before touching anything; no-op (and says so) when already
    current. Targets the luxe source repo regardless of where you run it."""
    import subprocess as sp

    from luxe import buildinfo

    root = buildinfo._repo_root()
    old, dirty = buildinfo.version_parts()
    console.print(f"[bold]luxe[/] [dim]{root}[/] · version {old}"
                  + (" [yellow](dirty — rebase will autostash)[/]" if dirty
                     else ""))

    with console.status("[dim]fetching origin…[/]"):
        fetched = buildinfo.fetch_origin(timeout_s=20)
    if not fetched:
        console.print("[red]✗ couldn't fetch origin[/] [dim](offline, or no "
                      "remote configured) — try again with network[/]")
        sys.exit(1)

    behind = buildinfo.behind_origin()
    if behind == 0:
        console.print("[green]✓ already current with origin/main[/]")
        return

    def _git(*args, timeout=120):
        return gitcmd.run(root, *args, timeout=timeout)

    log = _git("log", "--oneline", "HEAD..origin/main")
    console.print(f"[bold]{behind} commit(s) incoming:[/]")
    for line in log.stdout.strip().splitlines()[:15]:
        console.print(f"  [dim]{line}[/]")

    pulled = _git("pull", "--rebase")
    if pulled.returncode != 0:
        console.print(f"[red]✗ git pull --rebase failed:[/]\n"
                      f"[dim]{(pulled.stderr or pulled.stdout).strip()}[/]")
        sys.exit(1)

    if not no_sync:
        # chat = TUI; dev = pytest (the --code drill runs it via the venv
        # python, so it's a RUNTIME need on every host); analyzers = the
        # lint/typecheck/security tools shell-outs; web = playwright, after
        # the 2026-08-05 fleet deployment of web_page/render — every sync
        # WITHOUT it pruned the package and silently withheld the tools
        # (same failure shape as the 2026-07-30 dev prune that broke the
        # drill). The Chromium download stays per-host and outside the venv.
        extras = ["--extra", "chat", "--extra", "dev", "--extra", "analyzers",
                  "--extra", "web"]
        with console.status("[dim]uv sync (chat+dev+analyzers+web)…[/]"):
            try:
                synced = sp.run(["uv", "sync", *extras],
                                cwd=str(root), capture_output=True, text=True,
                                timeout=600)
            except FileNotFoundError:
                synced = None
        if synced is None:
            console.print("[yellow]⚠ uv not on PATH — run "
                          "`uv sync --extra chat --extra dev --extra "
                          "analyzers --extra web` in the repo yourself[/]")
        elif synced.returncode != 0:
            console.print(f"[yellow]⚠ uv sync failed:[/]\n"
                          f"[dim]{synced.stderr.strip()[-500:]}[/]")

    new, _ = buildinfo.version_parts()
    console.print(f"[bold][green]✓[/] {old} → {new}[/] [dim]— restart any "
                  "open chat/code sessions to pick it up[/]")


@main.command(name="smoke")
@click.option("--config", "config_path", default=None,
              help="Config YAML (default: configs/chat.yaml)")
@click.option("--backend", "backend_name", default=None,
              help="Run against this configured backends: entry (e.g. m5). "
                   "Drill models resolve from the TARGET host's manifest.")
@click.option("--base-url", default="",
              help="oMLX endpoint to smoke (default: the config's default backend)")
@click.option("--code", "code_drill", is_flag=True, default=False,
              help="Run the CODING drill instead: plant a bug + failing test "
                   "in a scratch repo, let the model fix it, verify with "
                   "pytest + git diff.")
@click.option("--chat", "chat_drill", is_flag=True, default=False,
              help="Run the CHAT drill instead: a read-only turn that must "
                   "read a file to answer.")
@click.option("--skip-fallback", is_flag=True, default=False,
              help="Skip the fallback-model leg (it pays a full weight swap).")
@click.option("--skip-tools", is_flag=True, default=False,
              help="Skip the tool-call turn.")
@click.option("--keep-loaded", is_flag=True, default=False,
              help="Leave the last smoked model resident.")
@click.option("--expect-model", default="",
              help="Identity preflight: fail (exit 2) unless the target "
                   "endpoint serves a model whose id contains this "
                   "substring. Run before n-rep acceptance nights — a "
                   "health check is not an identity check.")
@click.option("--model", "model_override", default=None,
              help="Drill this cached model instead of the manifest main "
                   "(--chat/--code only — e.g. the m5 capacity model, which "
                   "is a keep:, never a main). The default kit drill stays "
                   "manifest-driven.")
@click.option("--no-fix", "no_fix", is_flag=True, default=False,
              help="Diagnose only: never restart a stale local oMLX. By "
                   "default the kit drill self-repairs the one failure luxe "
                   "can fix (a server brew upgraded underneath) and re-runs.")
def smoke_cmd(config_path: str | None, backend_name: str | None,
              base_url: str, code_drill: bool, chat_drill: bool,
              skip_fallback: bool, skip_tools: bool, keep_loaded: bool,
              expect_model: str, model_override: str | None, no_fix: bool):
    """Aliveness drills for this host's fallback kit (minutes, not a bench).

    Default: manifest → weights → endpoint → catalog → one real turn + tool
    call on main → one turn on the fallback. `--code` / `--chat` run the
    agentic drills instead (combinable): real run_single turns against a
    planted scratch repo — the full coding pipeline, deterministically
    verified. `--backend m5` drills a remote host's models from here.
    Exit 0 = ready; exit 1 = something needs fixing (each line says what).
    """
    from luxe.chat.smoke import run_chat_drill, run_code_drill, run_smoke

    cfg = _chat_cfg(config_path)
    # `luxe smoke` picks its own endpoint (--base-url / the manifest), so the
    # name is validated but never promoted to default.
    _select_backend(cfg, backend_name, make_default=False)
    t0 = time.time()
    glyphs = {"pass": "[green]✓[/]", "warn": "[yellow]⚠[/]", "fail": "[red]✗[/]"}

    if expect_model:
        from luxe.chat.smoke import check_expected_model
        ok, detail = check_expected_model(cfg, expect_model,
                                          base_url=base_url or None,
                                          backend_name=backend_name)
        console.print(f"  {glyphs['pass' if ok else 'fail']} identity — {detail}")
        if not ok:
            sys.exit(2)

    reports = []
    if code_drill or chat_drill:
        if chat_drill:
            console.print("[bold]chat drill[/]")
            reports.append(run_chat_drill(cfg, backend_name=backend_name,
                                          base_url=base_url or None,
                                          model=model_override))
            for step in reports[-1].steps:
                console.print(f"  {glyphs[step.state]} {step.name} — {step.detail}")
        if code_drill:
            console.print("[bold]code drill[/]")
            reports.append(run_code_drill(cfg, backend_name=backend_name,
                                          base_url=base_url or None,
                                          model=model_override))
            for step in reports[-1].steps:
                console.print(f"  {glyphs[step.state]} {step.name} — {step.detail}")
    else:
        if model_override:
            console.print("[yellow]⚠ --model applies to --chat/--code drills "
                          "only; the kit drill is manifest-driven.[/]")
        reports.append(run_smoke(cfg, backend_name=backend_name,
                                 base_url=base_url or None,
                                 skip_fallback=skip_fallback,
                                 skip_tools=skip_tools))
        for step in reports[-1].steps:
            console.print(f"  {glyphs[step.state]} {step.name} — {step.detail}")
        # Self-repair (luxe.repair, 2026-09-11): the fallback kit must WORK
        # when reached for. A stale oMLX is the one failure luxe can fix on
        # its own, so when the drill fails with that signature (and the
        # endpoint is a local brew oMLX) restart it and drill again, loudly.
        # The second table is the verdict. --no-fix keeps the old
        # diagnose-only behaviour; a remote --backend never restarts anything
        # (repair_omlx refuses non-local endpoints itself).
        if reports[-1].failed and not no_fix:
            evidence = reports[-1].stale_evidence
            if evidence:
                rep = _smoke_self_repair(cfg, base_url or None, evidence,
                                         backend_name=backend_name)
                if rep.attempted:
                    console.print("[bold]after repair[/]")
                    reports = [run_smoke(cfg, backend_name=backend_name,
                                         base_url=base_url or None,
                                         skip_fallback=skip_fallback,
                                         skip_tools=skip_tools)]
                    for step in reports[-1].steps:
                        console.print(f"  {glyphs[step.state]} {step.name} — "
                                      f"{step.detail}")

    failed = any(r.failed for r in reports)
    from luxe.chat.smoke import endpoint_is_shared
    if not keep_loaded and not endpoint_is_shared(cfg, backend_name,
                                                  base_url or None):
        # Only unload an endpoint we OWN (B5): a shared/remote host's
        # residency is its own business — never unload a server another
        # session may be using (chat.sdd: remote drills never unload).
        try:
            cfg.build_backend(backend_name, "",
                              base_url=base_url or None).unload_all_loaded()
        except Exception:
            pass
    verdict = ("[red]NOT READY[/]" if failed else "[green]READY[/]")
    console.print(f"[bold]{verdict}[/] [dim]({time.time() - t0:.0f}s)[/]")
    sys.exit(1 if failed else 0)


def _smoke_self_repair(cfg, base_url: str | None, evidence: str, *,
                       backend_name: str | None = None):
    """Restart a stale local oMLX for `luxe smoke` / `luxe ready --fix` /
    `luxe repair`, narrating every step. Returns the RepairResult."""
    from luxe.repair import repair_omlx

    entry = cfg.backend_entry(backend_name or cfg.default_backend_name())
    url = base_url or entry.base_url
    backend = entry.build_backend("", base_url=url)
    console.print("[yellow]⟳ self-repair[/] — stale oMLX: restarting it "
                  "[dim](--no-fix to only diagnose)[/]")
    res = repair_omlx(base_url=url, health=backend.health,
                      error_text=evidence, engine=entry.engine)
    if not res.attempted:
        console.print(f"  [yellow]·[/] no restart: {res.reason}")
        return res
    for step in res.steps:
        console.print(f"  [dim]·[/] {step}")
    console.print(f"  {'[green]✓[/]' if res.ok else '[red]✗[/]'} {res.detail}")
    return res


def build_ready_doctor(cfg, repo_path: str):
    """Build the host-level `Doctor` for `luxe ready` — no REPL, no model.

    Reuses `/doctor`'s checks verbatim against a stand-in session so the two
    surfaces can never disagree; `hostwide_view` then restates the lines that
    only mean something inside a session. Split out of `ready_cmd` so tests can
    assert render parity with `/doctor` on the same inputs.
    """
    from luxe.chat import inspection
    from luxe import project as project_mod
    from luxe.chat.origin import host_for_endpoint
    from luxe.chat.session import ChatSession
    from luxe.chat.slots import SlotManager

    project = project_mod.resolve(repo_path)
    session = ChatSession(repo_path=project.root, project_kind=project.kind)
    # `luxe ready` is a DRILL, not a session: models and manifest resolve from
    # the host the active endpoint POINTS AT (chat.sdd drill rule, same as
    # smoke) — `--backend m5` judges m5's pair against m5's catalog. For the
    # local default this is short_hostname(), i.e. exactly the session rule.
    entry = cfg.backend_entry(cfg.default_backend_name())
    slots = SlotManager(cfg, manifest_host=host_for_endpoint(entry.base_url))
    doc = inspection.run_doctor(session, slots, project.root)
    return inspection.hostwide_view(doc)


@main.command(name="ready")
@click.option("--config", "config_path", default=None,
              help="Config YAML (default: configs/chat.yaml)")
@click.option("--backend", "backend_name", default=None,
              help="Check this configured backends: entry (e.g. m5) instead "
                   "of the default one.")
@click.option("--repo", default=".",
              help="Directory to judge the project checks against (default: cwd)")
@click.option("--fix", "fix", is_flag=True, default=False,
              help="If the oMLX build line is stale, restart the local server "
                   "and re-check (same repair `luxe smoke` runs by default).")
def ready_cmd(config_path: str | None, backend_name: str | None, repo: str,
              fix: bool):
    """Can I work right now? Point-in-time host preflight — seconds, no model.

    The same table `/doctor` prints inside a session: endpoint, oMLX build,
    key, model, weights, manifest, disk, update, git. Exit 0 = ready (warnings
    included), exit 1 = something is broken. Every ✗/! line carries a
    runnable fix. Offline-safe: the ≤4s `update` fetch is the only network
    call and degrades quietly.
    """
    from luxe.chat import inspection

    t0 = time.time()
    cfg = _chat_cfg(config_path)
    _select_backend(cfg, backend_name)

    doc = build_ready_doctor(cfg, str(Path(repo).expanduser()))
    worst = inspection.render_doctor(doc, console, title="luxe ready")
    if fix:
        from luxe.repair import is_stale_build_line
        stale = next((c for c in doc.checks
                      if is_stale_build_line(c.name, c.state, c.detail)),
                     None)
        if stale is None:
            console.print("[dim]· --fix: oMLX build is not stale, nothing to restart[/]")
        elif _smoke_self_repair(cfg, None, stale.detail,
                                backend_name=backend_name).attempted:
            console.print("[bold]after repair[/]")
            doc = build_ready_doctor(cfg, str(Path(repo).expanduser()))
            worst = inspection.render_doctor(doc, console, title="luxe ready")

    if worst == inspection.FAIL:
        console.print(f"[bold][red]NOT READY[/][/] "
                      f"[dim]({time.time() - t0:.0f}s)[/] — fix the ✗ lines "
                      "above")
        console.print("[dim]offline emergency card: `luxe outage`[/]")
        sys.exit(1)
    label = ("[green]READY[/]" if worst == inspection.OK
             else "[yellow]READY (warnings)[/]")
    console.print(f"[bold]{label}[/] [dim]({time.time() - t0:.0f}s)[/]")
    console.print("[dim]full generation drill: `luxe smoke` · agentic drill: "
                  "`luxe smoke --chat --code`[/]")
    sys.exit(0)


@main.command(name="repair")
@click.option("--config", "config_path", default=None,
              help="Config YAML (default: configs/chat.yaml)")
@click.option("--force", is_flag=True, default=False,
              help="Restart even when the build check is inconclusive "
                   "(still local, brew-installed oMLX only).")
def repair_cmd(config_path: str | None, force: bool):
    """Restart a stale local oMLX and wait for it (the one self-repair).

    A server left running across `brew upgrade` executes from a deleted
    Cellar tree: it passes health, lists its catalog, then fails every
    model load with a bogus `No module named …`. `luxe smoke` does this
    repair automatically; `luxe ready` names it; this is the explicit form.
    Refuses anything that is not that signature unless --force. Exit 0 =
    healthy on the installed build, 1 = restart did not recover it,
    2 = refused (not stale / remote / not brew / not oMLX). The 5-minute
    restart cooldown is per PROCESS, so it never refuses a fresh `luxe
    repair` — it bounds the in-process callers (smoke's re-drill, a chat
    session) instead.
    """
    from luxe.repair import repair_omlx

    cfg = _chat_cfg(config_path)
    entry = cfg.backend_entry(cfg.default_backend_name())
    backend = entry.build_backend("")
    console.print(f"[dim]· checking oMLX at {entry.base_url}…[/]")
    res = repair_omlx(base_url=entry.base_url, health=backend.health,
                      engine=entry.engine, force=force)
    if not res.attempted:
        console.print(f"[yellow]· no restart: {res.reason}[/]")
        if not force:
            console.print("[dim]  `luxe repair --force` restarts it anyway[/]")
        sys.exit(2)
    for step in res.steps:
        console.print(f"  [dim]·[/] {step}")
    if res.ok:
        console.print(f"[green]✓ {res.detail}[/]")
        console.print("[dim]next: `luxe smoke` for a real turn[/]")
        sys.exit(0)
    console.print(f"[red]✗ {res.detail}[/] — `brew services info omlx`, "
                  "`tail ~/.omlx/omlx.log`")
    sys.exit(1)


@main.command(name="outage")
@click.option("--plain", is_flag=True, default=False,
              help="Print the raw markdown (no Rich rendering).")
def outage_cmd(plain: bool):
    """Print the offline emergency card (OUTAGE.md).

    Zero network, zero model, no config: it works with oMLX stopped and the
    link down. `luxe ready` points here when it says NOT READY.
    """
    from luxe.outage import load_card

    text = load_card()
    if plain or not console.is_terminal:
        click.echo(text)
    else:
        from rich.markdown import Markdown
        console.print(Markdown(text))
    sys.exit(0)


@main.command(name="net")
@click.option("--host", default=None,
              help="Hostname for the public ladder (default: a public anchor)")
@click.option("--config", "config_path", default=None,
              help="Pipeline config (default: the chat config)")
@click.option("--watch", "watch_s", default=0, type=int,
              help="Re-probe every N seconds; print verdict TRANSITIONS only "
                   "(Ctrl-C to stop). Transitions also append to "
                   "~/.luxe/netwatch.log.")
def net_cmd(host: str | None, config_path: str | None, watch_s: int):
    """Layered network report: DNS → TCP → TLS → HTTP(S) + captive-portal
    check + every configured `backends:` endpoint. Deterministic (no model),
    every probe hard-bounded — total wall is a few seconds. The verdict names
    the broken LAYER (tls-blocked, captive-portal, dns-broken, …) instead of
    describing symptoms.
    """
    from luxe import netdiag

    try:
        cfg = _chat_cfg(config_path)
    except Exception:
        cfg = None
    anchor = host or netdiag.ANCHOR_HOST

    def _render(report) -> None:
        textfmt.render_ok_lines(console, netdiag.render_lines(report))
        style = "green" if report.ladder.verdict == netdiag.V_OK else "yellow"
        console.print(f"[{style}]verdict: {report.ladder.verdict}[/] — "
                      f"{report.ladder.advice}")

    report = netdiag.full_report(cfg, host=anchor)
    _render(report)
    if not watch_s:
        sys.exit(0 if report.ladder.verdict == netdiag.V_OK else 1)

    # Watch mode: the question on a bad network is "when does it change?"
    # (session 5bb630813c21: HTTPS silently recovered mid-flight). Quiet
    # while stable; a verdict transition prints + appends to the log.
    log_path = luxe_home() / "netwatch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    last = report.ladder.verdict
    console.print(f"[dim]watching every {watch_s}s — verdict transitions "
                  f"only (log: {log_path}) · Ctrl-C to stop[/]")
    try:
        while True:
            time.sleep(max(watch_s, 5))
            report = netdiag.full_report(cfg, host=anchor)
            now = report.ladder.verdict
            if now != last:
                stamp = time.strftime("%H:%M:%S")
                console.print(f"[bold]{stamp} {last} → {now}[/] — "
                              f"{report.ladder.advice}")
                try:
                    with log_path.open("a", encoding="utf-8") as fh:
                        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                                 f"{last} -> {now}\n")
                except OSError:
                    pass
                last = now
    except KeyboardInterrupt:
        console.print(f"[dim]stopped — last verdict: {last}[/]")
        sys.exit(0)


@main.command(name="planeproxy")
@click.option("--check", type=click.Choice(["status", "doctor", "both"]),
              default="both", help="Which probe to run (default: both)")
@click.option("--json", "as_json", is_flag=True,
              help="Emit the raw report as JSON instead of the rendered lines")
def planeproxy_cmd(check: str, as_json: bool):
    """Diagnose the planeproxy SSH tunnel (read-only). Runs its own
    `status --json` / `doctor --json` under a hard deadline and classifies
    the result into a verdict with the one fix that matters (host-key
    mismatch, captive portal, stranded routing, …). Never starts or stops
    the tunnel. Exit 0 when healthy, 1 otherwise.
    """
    from luxe import planeproxy as pp

    report = pp.full_report(check=check)
    if as_json:
        import dataclasses
        import json as json_mod
        click.echo(json_mod.dumps(dataclasses.asdict(report), indent=2))
    else:
        textfmt.render_ok_lines(console, pp.render_lines(report))
    sys.exit(0 if report.verdict == pp.PP_OK else 1)


@main.command(name="claudecode")
@click.option("--check", type=click.Choice(["status", "net", "all"]),
              default="all", help="Which probes to run (default: all)")
@click.option("--repo", default=None,
              help="Also inspect this project's .claude/settings*.json")
@click.option("--json", "as_json", is_flag=True,
              help="Emit the raw report as JSON instead of the rendered lines")
def claudecode_cmd(check: str, repo: str | None, as_json: bool):
    """Diagnose Claude Code (the `claude` CLI), read-only.

    Answers the question a luxe fallback session gets asked: which billing
    path is each running session actually on — Max-subscription login or
    Platform API key — and what is overriding it (ANTHROPIC_BASE_URL, an
    `env:` block, an apiKeyHelper, Bedrock/Vertex). Also reports settings-file
    validity, the install, and metadata for the recent sessions.

    Environment variables are reported by NAME only and Keychain lookups are
    metadata-only, so no secret is ever exposed; conversation content is never
    read. Never launches, kills, or reconfigures Claude Code. Exit 0 when
    healthy, 1 otherwise.
    """
    from luxe import claudecode as cc

    report = cc.full_report(check=check, repo_path=repo)
    if as_json:
        import dataclasses
        import json as json_mod
        # asdict recurses into the netdiag LadderReport too (also a dataclass),
        # so the ladder rungs survive the JSON form.
        click.echo(json_mod.dumps(dataclasses.asdict(report), indent=2))
    else:
        textfmt.render_ok_lines(console, cc.render_lines(report))
    sys.exit(0 if report.verdict == cc.CC_OK else 1)
