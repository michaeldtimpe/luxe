"""`/doctor` for `luxe chat` and `luxe ready`: preflight the things that
silently break a session.

`run_doctor` returns ordered `Check`s (endpoint → build → key → model →
weights → manifest → disk → update → index → freshness → tree → mode → TUI);
`render_doctor` is the ONE renderer both surfaces print through. Nothing here
may raise and nothing mutates a repo or a session. `chat.inspection`
re-exports every public name (and the privates tests reach), so
`chat.inspection.render_doctor` — the path `luxe ready` and luxe.sdd name —
keeps working. Code that stubs a lookup must patch THIS module.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from luxe.chat import origin as origin_mod
from luxe.chat.gitdiff import session_diff

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    state: str = OK          # ok | warn | fail
    detail: str = ""
    fix: str = ""            # what the user can do about it


@dataclass
class Doctor:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, state: str, detail: str = "", fix: str = "") -> None:
        self.checks.append(Check(name=name, state=state, detail=detail, fix=fix))

    @property
    def worst(self) -> str:
        states = {c.state for c in self.checks}
        return FAIL if FAIL in states else WARN if WARN in states else OK


_MIN_FREE_GB = 40      # a champion swap writes ~28 GB of weights + KV headroom


def _manifest_checks(doc: Doctor, slots, reachable: bool,
                     served: list[str] | None = None) -> None:
    """Fallback-kit checks: manifest resolution, degrade state, weight presence.

    The 2026-07-29 incident this exists for: champion weights silently deleted,
    chat started fine and returned nothing. Nothing owned the assertion "these
    weights should be here" — now this does. Never raises. `served` is the
    catalog `run_doctor` already fetched (None = unknown / not fetched).
    """
    try:
        cfg = slots.cfg
        entry = slots.active_entry()
        is_omlx, engine_label = slots.is_omlx(), slots.engine_label()
        if slots.is_billable():
            # A billable endpoint is never a manifest main/fallback (luxe.sdd
            # cloud carve-out), so "add a hosts: entry for it" would be a fix
            # the contract forbids. Its catalog is the provider's, not a host's.
            doc.add("host manifest", OK,
                    f"n/a — {engine_label} is billable; manifests describe "
                    "local weights only")
            return
        # The SlotManager already resolved WHOSE manifest governs (this host
        # for sessions; the endpoint's host for `luxe ready --backend <name>`,
        # the drill rule) — never re-derive locally here, that was the bug
        # that judged m1's pair against m5's catalog.
        manifest = getattr(slots, "manifest", None)
        if not getattr(cfg, "hosts", None):
            doc.add("host manifest", OK, "none configured (single-model default)")
            return
        if manifest is None:
            from luxe.config import short_hostname
            looked_for = (getattr(slots, "_manifest_host", None)
                          or short_hostname() or "(unknown)")
            doc.add("host manifest", WARN,
                    f"no hosts: entry matches {looked_for!r}",
                    "add this host under hosts: in configs/chat.yaml")
            return
        fb = manifest.fallback or "—"
        doc.add("host manifest", OK, f"main {manifest.main} · fallback {fb}")

        if getattr(slots, "degraded_from", None):
            doc.add("degraded", WARN,
                    f"running fallback {slots.degraded_to} — "
                    f"{slots.degraded_from} unavailable",
                    f"restore it (`luxe pull {slots.degraded_from}`), then "
                    f"`/model chat {slots.degraded_from}`")

        # Weight presence on the LOCAL oMLX store — a remote endpoint's disk
        # is its own doctor's problem; served-catalog coverage is checked
        # below. `~/.omlx/models` is the only layout luxe knows: llama-server
        # (neo) loads the paths in its preset, so reading the oMLX store
        # there FAILED a working host's main as "not in the store".
        from luxe.chat.origin import endpoint_is_local
        local = endpoint_is_local(getattr(slots.backend, "base_url", ""))
        if local and is_omlx:
            for mid, state, detail, fix in weights_checks(
                    manifest.all_models(), main=manifest.main,
                    fallback=manifest.fallback):
                doc.add(f"weights:{mid}", state, detail, fix)
        elif local:
            doc.add("weights:store", OK,
                    f"n/a — {engine_label} loads the files its own preset "
                    "names (a real turn is the proof: `luxe smoke`)")
        if reachable and served:
            served_set = set(served)
            served_fix = endpoint_fixes(entry)["served"]
            for mid in (manifest.main, manifest.fallback):
                if mid and mid not in served_set:
                    doc.add(f"served:{mid}", WARN, "not in the server catalog",
                            served_fix)
    except Exception as e:
        doc.add("host manifest", WARN, f"check errored: {e}")


def _weights_fix(mid: str, state: str, hf_repo_for) -> str:
    """Runnable fix for a missing/dangling manifest model.

    A DANGLING entry's link text still names the HF repo it pointed at, and a
    full `org/Name` is what routes `luxe pull` to HuggingFace — a bare name
    can only be found on a mounted volume, which is not where a wiped HF
    cache's weights live. Name the repo whenever the link gives it away.
    """
    repo = hf_repo_for(mid) if state == "dangling" else ""
    if repo:
        return (f"`luxe pull {repo}` (re-fetch from HuggingFace — the "
                "dangling entry pointed there)")
    return f"`luxe pull {mid}` (kappa mount; `luxe pull <org>/{mid}` for HF)"


def weights_checks(models: list[str], *, main: str, fallback: str = ""
                   ) -> list[tuple[str, str, str, str]]:
    """`(model, state, detail, fix)` for each of `models` in the LOCAL oMLX
    store — the one implementation behind `/doctor`'s `weights:<id>` lines
    and `luxe smoke`'s `weights <id>` steps (they used to carry a copy each,
    and smoke's fix string drifted from doctor's).

    The main missing is a FAIL (the session cannot start on it); anything
    else is a WARN. Callers decide whether the store applies at all — it is
    the layout of a LOCAL `engine: omlx` endpoint only.
    """
    from luxe import modelstore as ms

    out: list[tuple[str, str, str, str]] = []
    for mid in models:
        state = ms.model_state(mid)
        role = ("main" if mid == main
                else "fallback" if mid == fallback else "keep")
        if state == "ok":
            out.append((mid, OK, f"{role} · on disk", ""))
            continue
        why = ("store entry dangles into a wiped cache"
               if state == "dangling" else "not in the store")
        out.append((mid, FAIL if role == "main" else WARN, f"{role} · {why}",
                    _weights_fix(mid, state, ms.hf_repo_for)))
    return out


#: Deadline for `/doctor`'s one catalog GET, which doubles as its liveness
#: probe. The client's own read timeout is sized for GENERATION (600s local,
#: 2400s for the m5 entry) — wrong for a question asked during an outage.
_LIVENESS_TIMEOUT_S = 4.0


def _fetch_catalog(backend) -> list[str]:
    """ONE bounded `/v1/models` GET: liveness and catalog in a single request.

    `/doctor` used to ask the same endpoint the same question up to four
    times (health, available_models, the manifest's served check, and the
    remote-origin lookup) — four round-trips over a degraded link. Raises on
    failure; the caller turns that into the endpoint line.
    """
    try:
        return list(backend.list_models(timeout_s=_LIVENESS_TIMEOUT_S))
    except TypeError:
        # A stand-in predating the bound; it is a fake, nothing to hang on.
        return list(backend.list_models())


def endpoint_fixes(entry) -> dict[str, str]:
    """Engine-aware fix strings for an endpoint: `start` (the server is not
    there), `restart` (it is there and not answering), `served` (a manifest
    model is missing from its catalog).

    Shared by `/doctor`, `luxe ready` and `luxe smoke` so no surface prints
    `brew services restart omlx` at a llama-server host (neo) or a cloud
    endpoint again. `entry` None = the oMLX defaults. Never raises.
    """
    try:
        is_omlx = True if entry is None else entry.is_omlx()
        is_cloud = False if entry is None else entry.is_openrouter()
        label = "oMLX" if entry is None else entry.engine_label()
    except Exception:
        is_omlx, is_cloud, label = True, False, "oMLX"
    key_env = getattr(entry, "api_key_env", "") or "OMLX_API_KEY"
    # A cloud endpoint has no local process: "start the server" is the one fix
    # that cannot possibly apply, and the real causes are the link and the key.
    cloud_fix = (f"check the network (`/net`) and that ${key_env} is set "
                 "(env or `~/.luxe/secrets.env`); `/usage` shows whether the "
                 "account still has credits, `/backend <other>` to run local")
    start = (cloud_fix if is_cloud else
             "start it (`brew services start omlx`) or `/backend <other>`"
             if is_omlx else
             "start the server (on neo: `launchctl bootstrap gui/$UID "
             "~/Library/LaunchAgents/com.micromind.llama-server.plist`), "
             "or `/backend <other>`")
    restart = (cloud_fix if is_cloud else
               "`brew services restart omlx`, or `/backend <other>`"
               if is_omlx else
               "restart the server (on neo: `launchctl kickstart -k "
               "gui/$UID/com.micromind.llama-server`), or `/backend <other>`")
    served = ("restart oMLX after provisioning (`brew services restart omlx`)"
              if is_omlx else
              f"add the model to {label}'s preset and restart it, then re-run")
    return {"start": start, "restart": restart, "served": served}


def stale_build_check(base_url: str, *, is_omlx: bool = True
                      ) -> tuple[str, str, str] | None:
    """`(state, detail, fix)` for whether the local oMLX is running the Cellar
    tree brew installed, or None when there is nothing to say.

    One implementation for `/doctor`'s and `luxe smoke`'s `oMLX build` line.
    A stale process is a WARN, not a FAIL: it may still be serving everything
    asked of it, and smoke/doctor's real checks will fail on their own if it
    is not. The value here is that when they DO fail, the cause is already on
    screen instead of costing another half hour. None on a non-oMLX engine
    (nothing brew-installed to be stale about), a remote endpoint (its
    process table is its own problem), and an INCONCLUSIVE verdict (not
    running / not brew-installed — other checks own that). A check that could
    not run is OK "unchecked". Never raises — doctor runs during outages.
    """
    if not is_omlx:
        return None
    try:
        from luxe.chat.origin import endpoint_is_local
        if not endpoint_is_local(base_url):
            return None
        from luxe.staleproc import check_omlx

        check = check_omlx()
        if not check.conclusive:
            return None
        if check.stale:
            return WARN, check.detail, check.fix
        return OK, check.detail, ""
    except Exception as e:
        return OK, f"unchecked ({e})", ""


def _add_stale_check(doc, base_url: str, *, is_omlx: bool = True) -> None:
    """Add the `oMLX build` line (`stale_build_check`) when it has one."""
    line = stale_build_check(base_url, is_omlx=is_omlx)
    if line is not None:
        doc.add("oMLX build", *line)


def _add_web_check(doc, session) -> None:
    """Report the /web surface. Never networked — the offline-purity contract
    allows exactly one networked doctor line (`update`), and doctor runs during
    outages. Key lookup and the Chromium probe are both local."""
    if not getattr(session, "web_enabled", False):
        doc.add("web", OK, "off (no network egress from tools)",
                "`/web` to enable web_fetch/web_search/web_answer")
        return
    from luxe.web import search as _search
    from luxe.web.browser import availability as _avail

    from luxe.web import answers as _answers

    bits = ["web_fetch on"]
    provider = _search.active_provider()
    bits.append(f"search via {provider[0].name}" if provider else "search withheld (no key)")
    bits.append("answers on" if _answers.configured()
                else "answers withheld (no key)")
    a = _avail()
    bits.append("render ready" if a.ok else "render unavailable")
    state = OK if (provider and a.ok) else WARN
    fix = ""
    if not provider:
        fix = _search.missing_key_message()
    elif not a.ok:
        fix = a.fix
    doc.add("web", state, " · ".join(bits), fix)


def render_doctor(doc: Doctor, console, *, title: str = "Doctor") -> str:
    """Print `doc` as the glyph table + `→ fix` lines and return the verdict key.

    Single-sourced so `/doctor` (in-session) and `luxe ready` (host-level,
    no REPL) print IDENTICALLY — two renderers would drift, and the whole
    point of `ready` is that the operator sees the same table under pressure
    that they see in a session.
    """
    glyphs = {OK: "[green]✓[/]", WARN: "[yellow]![/]", FAIL: "[red]✗[/]"}
    width = max((len(c.name) for c in doc.checks), default=0)
    console.print(f"[bold]{title}[/]")
    for c in doc.checks:
        console.print(f"  {glyphs.get(c.state, '·')} "
                      f"[dim]{c.name.ljust(width)}[/]  {c.detail}")
        if c.fix and c.state != OK:
            console.print(f"    [dim]→ {c.fix}[/]")
    return doc.worst


def hostwide_view(doc: Doctor) -> Doctor:
    """Restate the SESSION-scoped checks for a host-level run (`luxe ready`).

    `ready` builds a stand-in session (write/bash/web off, no index) purely so
    `run_doctor` can be reused verbatim. Left alone, those lines would report
    facts about the stand-in and read as claims about the session the user is
    about to start — "read-only", "search index not built" — which `ready`
    knows nothing about. Restate them as the startup defaults and drop their
    fix lines so they can never colour the verdict. Mutates and returns `doc`.
    """
    restated = {
        "mode": ("n/a outside a session (chat starts read-only · bash "
                 "allowlisted; `luxe code` and `--dev` start with write on)"),
        "web": "n/a outside a session (chat starts with web off)",
        "search index": ("not built (`luxe ready` doesn't index; chat builds "
                         "it at startup)"),
    }
    for c in doc.checks:
        if c.name in restated:
            c.state = OK
            c.detail = restated[c.name]
            c.fix = ""
    return doc


def run_doctor(session, slots, repo_path: str) -> Doctor:
    """Preflight the things that silently break a chat session.

    Ordered so the first FAIL is the one to fix first: an unreachable endpoint
    makes every later check meaningless. Each group is its own helper; none
    of them may raise.
    """
    doc = Doctor()
    reachable, served = _endpoint_checks(doc, slots)
    _key_check(doc, slots)
    _model_checks(doc, slots, reachable, served)
    # Host manifest (fallback kit): the declared main/fallback pair for this
    # machine, and whether its weights are actually on disk. Config-only plus
    # local filesystem — runs in no-project sessions too. Guarded throughout
    # (doctor contract: nothing here may raise).
    _manifest_checks(doc, slots, reachable, served)
    _disk_check(doc)
    _update_check(doc)
    _project_checks(doc, session, repo_path)
    _session_checks(doc, session)
    return doc


def _endpoint_checks(doc: Doctor, slots) -> tuple[bool, list[str] | None]:
    """The endpoint line + the stale-build line under it. Returns
    `(reachable, served)` — the one catalog every later check reuses."""
    import httpx

    backend = slots.backend
    base_url = getattr(backend, "base_url", "") or "?"
    name = getattr(slots, "backend_name", "?")
    # Which serving stack is behind this URL. oMLX unless `backends:` says
    # otherwise, so every host but neo is unchanged. It switches only the
    # DIAGNOSTIC lines — a fix that reads `brew services start omlx` is
    # worse than no fix at all on a box with no omlx formula.
    is_omlx, engine_label = slots.is_omlx(), slots.engine_label()
    fixes = endpoint_fixes(slots.active_entry())

    # Liveness AND catalog from one bounded GET (see `_fetch_catalog`). A
    # non-200 is "there but not answering" (restart); anything else — refused,
    # timed out, unroutable — is "not there" (start).
    reachable = False
    served: list[str] | None = None
    try:
        served = _fetch_catalog(backend)
        reachable = True
    except httpx.HTTPStatusError as e:
        doc.add(f"{engine_label} endpoint", FAIL,
                f"{name} {base_url} not responding "
                f"(HTTP {e.response.status_code})", fixes["restart"])
    except Exception as e:
        doc.add(f"{engine_label} endpoint", FAIL,
                f"{name} {base_url}: {str(e) or type(e).__name__}",
                fixes["start"])
    else:
        doc.add(f"{engine_label} endpoint", OK, f"{name} {base_url}")

    # Stale-process check. Placed directly under the endpoint because it
    # EXPLAINS the checks below it: a server running a Cellar tree brew
    # deleted answers health and lists its catalog, then fails the next lazy
    # import with an error naming whatever module it happened to reach for
    # (lessons.md 2026-08-03 and 2026-08-04). Local endpoints only, and
    # offline-pure — pgrep/lsof/stat, no network.
    _add_stale_check(doc, base_url, is_omlx=is_omlx)
    return reachable, served


def _key_check(doc: Doctor, slots) -> None:
    key_env = getattr(slots.active_entry(), "api_key_env", "") or "OMLX_API_KEY"
    if getattr(slots.backend, "api_key", ""):
        doc.add("API key", OK, "present")
    elif slots.is_cloud():
        # FAIL, not WARN: on a cloud endpoint every single request 401s without
        # it, so this is the whole session, not a caveat. The env var is named;
        # its VALUE never appears here (chat.sdd Must-not).
        doc.add("API key", FAIL, f"no {key_env} resolved for this endpoint",
                f"`echo '{key_env}=<key>' >> ~/.luxe/secrets.env` "
                "(or export it in this shell), then restart the session")
    elif slots.is_omlx():
        # The ENTRY's variable, not a hard-coded OMLX_API_KEY: the m5 entry
        # reads OMLX_API_KEY_M5, and writing the other name fixes nothing.
        doc.add("API key", WARN, "no key resolved for this endpoint",
                f"`echo '{key_env}=<key>' >> ~/.luxe/secrets.env` "
                "(or export it in this shell)")
    else:
        # llama-server is keyless unless started with --api-key. A standing
        # yellow line whose fix changes nothing trains people to ignore the
        # yellow lines that matter.
        doc.add("API key", OK, f"not required by {slots.engine_label()}")


def _model_checks(doc: Doctor, slots, reachable: bool,
                  served: list[str] | None) -> None:
    """The chat model is served here, and where its weights live."""
    model = slots.model_for("chat")
    if not reachable:
        doc.add("chat model", WARN, f"{model} (unverified — endpoint down)",
                "fix the endpoint above first, then re-run")
        return
    try:
        # Same roster filter as `SlotManager.available_models`, applied
        # to the catalog already in hand instead of a second GET.
        available = set(slots.cfg.visible(served or [],
                                          entry=slots.active_entry()))
    except Exception:
        try:
            available = set(slots.available_models())
        except Exception:
            available = set()
    if available and model not in available:
        doc.add("chat model", FAIL, f"{model} is not served here",
                f"`/model chat <id>`, or `luxe pull {model}`")
    else:
        doc.add("chat model", OK, model)
    _origin_check(doc, slots, model, served)


def _origin_check(doc: Doctor, slots, model: str,
                  served: list[str] | None) -> None:
    backend = slots.backend
    engine_label = slots.engine_label()
    org = origin_mod.cached_origin_for(backend, model)
    if org.kind == "unknown":
        try:
            org = origin_mod.origin_for(backend, model, served=served)
        except Exception:
            pass
    if slots.is_cloud():
        # Provider-hosted: there is nothing on this disk to check and
        # nothing to `luxe pull`. The fact worth stating is that the turns
        # are billed, which no local endpoint has to say.
        doc.add("weights", OK,
                f"provider-hosted by {engine_label} — billable per token", "")
    elif org.kind == "network":
        doc.add("weights", WARN, f"{org.glyph} {org.detail}",
                f"`luxe pull {model}` to copy them to local disk "
                "(loading over the network makes the first turn slow)")
    elif org.kind == "remote":
        doc.add("weights", WARN, f"{org.glyph} served by {org.detail}",
                f"`luxe pull {model}` here, then `--backend local`, if "
                "prompts should not cross the network")
    elif org.kind == "local":
        doc.add("weights", OK, f"{org.glyph} local disk")
    elif slots.is_omlx():
        doc.add("weights", WARN, "location unreported by oMLX")
    else:
        # llama-server has no `/v1/models/status`, so there is no path to
        # report and nothing to fix; it loads what its own preset names.
        doc.add("weights", OK,
                f"{engine_label} reports no model path (its preset owns it)")


def _disk_check(doc: Doctor) -> None:
    from luxe.modelstore import human_bytes

    try:
        free = shutil.disk_usage(Path.home()).free
        if free < _MIN_FREE_GB * 1024**3:
            doc.add("disk", WARN, f"{human_bytes(free)} free",
                    "`luxe pull --list` then `luxe pull <model> --remove` an "
                    f"unused model (a swap wants ~{_MIN_FREE_GB} GB of headroom)")
        else:
            doc.add("disk", OK, f"{human_bytes(free)} free")
    except OSError as e:
        doc.add("disk", WARN, str(e))


def _update_check(doc: Doctor) -> None:
    """The ONE doctor line allowed to touch the network (a short git fetch on
    the luxe source repo). Startup banners stay offline-pure; /doctor is
    exactly where a round-trip is tolerable. Offline is NOT a warning: doctor
    runs during outages by design."""
    try:
        from luxe import buildinfo
        if buildinfo.fetch_origin(timeout_s=4):
            behind = buildinfo.behind_origin()
            if behind:
                doc.add("update", WARN,
                        f"{behind} commit(s) behind origin/main",
                        "`luxe update`")
            else:
                doc.add("update", OK, "current with origin/main")
        else:
            doc.add("update", OK, "unchecked (offline or no remote)")
    except Exception as e:
        doc.add("update", OK, f"unchecked ({e})")


def _project_checks(doc: Doctor, session, repo_path: str) -> None:
    """Search index, its freshness, and the working tree.

    The index is built at startup from the repo HEAD; a moved HEAD means the
    model is searching a tree that no longer matches the files it will read.
    A session with no project deliberately has none — that's a state, not a
    fault, so it reports OK with the escape hatch rather than a warning.
    """
    from luxe import search as search_mod

    if getattr(session, "project_kind", "git") == "none":
        doc.add("project", OK, f"none attached ({repo_path or 'cwd'})",
                "`/project <path>` or `/index` to enable code search")
        doc.add("search index", OK, "not built (no project)")
        return
    if not repo_path:
        doc.add("repo", WARN, "no repo path for this session")
        return
    from luxe.gitkit.health import current_head, is_git_repo

    index = search_mod.get_index()
    if index is None:
        doc.add("search index", WARN, "not built",
                "`/index` in chat, or start with `luxe chat --repo <path>`")
    else:
        doc.add("search index", OK, f"{len(getattr(index, 'paths', []))} files")
    if not is_git_repo(repo_path):
        doc.add("git", WARN, "not a git repo — `/diff` and git tools "
                             "won't work here",
                f"`git -C {repo_path} init` if it should be one")
        return
    head = current_head(repo_path)
    indexed = getattr(session, "index_head", "") or ""
    if indexed and head and not head.startswith(indexed) \
            and not indexed.startswith(head):
        doc.add("index freshness", WARN,
                f"indexed at {indexed}, HEAD is now {head}",
                "`/index` to reindex (or restart chat)")
    else:
        doc.add("index freshness", OK, head or "(no commits)")
    diffs, err = session_diff(repo_path)
    if err:
        doc.add("working tree", WARN, err)
    elif diffs:
        doc.add("working tree", OK, f"{len(diffs)} file(s) changed "
                                    "(`/diff` to see)")
    else:
        doc.add("working tree", OK, "clean")


def _session_checks(doc: Doctor, session) -> None:
    """The session-scoped tail: mode, web, TUI. ONE copy for every session
    shape — the no-project path used to carry its own, whose mode line had
    lost the `/write` fix (2026-10 review)."""
    mode = ("write on" if session.write_enabled else "read-only")
    bash = ("unrestricted" if session.unrestricted_bash else "allowlisted")
    doc.add("mode", OK, f"{mode} · bash {bash}",
            "" if session.write_enabled else "`/write` to let the model edit files")
    _add_web_check(doc, session)

    try:
        import textual  # noqa: F401
        doc.add("TUI", OK, "textual installed")
    except ImportError:
        doc.add("TUI", WARN, "textual missing — line REPL fallback",
                "`uv sync --extra chat`")
