"""Session introspection for `luxe chat`: `/export`, `/diff`, `/doctor`.

The logic lives here as pure-ish functions so it can be unit-tested without a
console, a model, or a live oMLX: `commands.py` keeps only the rendering. All
three are read-only — nothing here mutates a repo or a session.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from luxe.chat import origin as origin_mod
from luxe.memory import session as session_store

# --- /export ----------------------------------------------------------------


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def transcript_markdown(meta, records: list[dict]) -> str:
    """Render a session transcript as Markdown.

    Reads the persisted records (not in-memory turns) so `/export` works after
    `/resume` and captures interrupted turns too. `attachment` records are
    summarised rather than inlined — a 48 KB file pasted back into the export
    would bury the conversation.
    """
    started = getattr(meta, "created_at", 0.0) or 0.0
    lines: list[str] = [
        f"# luxe chat — {getattr(meta, 'session_id', '')}",
        "",
        f"- **Repo:** `{getattr(meta, 'repo_path', '') or '(none)'}`",
    ]
    if started:
        lines.append(f"- **Started:** {_stamp(started)}")
    backend_name = getattr(meta, "backend_name", "") or ""
    base_url = getattr(meta, "base_url", "") or ""
    if backend_name or base_url:
        lines.append(f"- **Backend:** {backend_name} `{base_url}`".rstrip())
    slot_models = getattr(meta, "slot_models", None) or {}
    if slot_models:
        lines.append("- **Models:** "
                     + ", ".join(f"{k}: `{v}`" for k, v in slot_models.items()))

    turns = 0
    body: list[str] = []
    for rec in records:
        kind = rec.get("kind", "")
        text = (rec.get("text") or "").rstrip()
        when = _stamp(rec.get("ts", 0.0)) if rec.get("ts") else ""
        if kind == "user":
            turns += 1
            body += ["", "---", "", f"### {turns} · you"
                     + (f"  <sub>{when}</sub>" if when else ""), "", text or "_(empty)_"]
        elif kind == "assistant":
            meta_bits = []
            if rec.get("steps"):
                meta_bits.append(f"steps {rec['steps']}")
            if rec.get("tool_calls"):
                meta_bits.append(f"tools {rec['tool_calls']}")
            if rec.get("backend"):
                meta_bits.append(str(rec["backend"]))
            if rec.get("interrupted"):
                meta_bits.append("INTERRUPTED")
            suffix = f"  <sub>{' · '.join(meta_bits)}</sub>" if meta_bits else ""
            body += ["", f"### {turns} · luxe{suffix}", "", text or "_(no reply)_"]
        elif kind == "attachment":
            paths = rec.get("paths") or ([rec["path"]] if rec.get("path") else [])
            label = ", ".join(f"`{p}`" for p in paths) or "(files)"
            body += ["", f"> attached: {label}"]
        elif kind == "error":
            # Failed turns are part of the story (2026-07-30): a session whose
            # question got no answer must not export as if nothing happened.
            model = f" ({rec['model']})" if rec.get("model") else ""
            body += ["", f"> ⚠ turn failed{model}: {text or '(no detail)'}"]

    lines.append(f"- **Turns:** {turns}")
    return "\n".join(lines + body).rstrip() + "\n"


def default_export_path(session_id: str) -> Path:
    """Where `/export` writes when given no path: beside the transcript it came
    from, so the session directory stays self-contained."""
    return session_store.session_dir(session_id) / "transcript.md"


def export_transcript(session_id: str, dest: str | Path | None = None) -> Path:
    """Write the Markdown transcript and return the path written.

    Raises `FileNotFoundError` for an unknown session and `OSError` if the
    destination isn't writable — callers report both.
    """
    loaded = session_store.load_session(session_id)
    if loaded is None:
        raise FileNotFoundError(f"no session {session_id!r}")
    meta, records = loaded
    out = Path(dest).expanduser() if dest else default_export_path(session_id)
    if out.is_dir():
        out = out / f"luxe-chat-{session_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(transcript_markdown(meta, records))
    return out


# --- /diff ------------------------------------------------------------------


@dataclass
class FileDiff:
    path: str
    added: int = 0
    removed: int = 0
    untracked: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.untracked)


def _git(repo: str | Path, *args: str, timeout: float = 10.0) -> tuple[bool, str]:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return proc.returncode == 0, (proc.stdout if proc.returncode == 0
                                 else proc.stderr).strip()


def session_diff(repo_path: str, paths: list[str] | None = None
                 ) -> tuple[list[FileDiff], str]:
    """Per-file +/- counts for `paths` (or the whole working tree when None).

    Compares against HEAD so staged AND unstaged edits both show; untracked
    files are reported as new rather than silently omitted (a session that
    scaffolded a file has changed the repo even though git has no baseline for
    it). Returns (diffs, error) — `error` non-empty means git couldn't answer.
    """
    if not repo_path:
        return [], "no repo for this session"
    args = ["diff", "--numstat", "HEAD"]
    if paths:
        args += ["--", *paths]
    ok, out = _git(repo_path, *args)
    if not ok:
        # No commits yet → no HEAD to diff against; fall back to the index.
        ok, out = _git(repo_path, "diff", "--numstat",
                       *(["--", *paths] if paths else []))
        if not ok:
            return [], out or "git diff failed"

    diffs: dict[str, FileDiff] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        diffs[path] = FileDiff(
            path=path,
            added=0 if added == "-" else int(added or 0),
            removed=0 if removed == "-" else int(removed or 0),
        )

    ok, status = _git(repo_path, "status", "--porcelain", "--untracked-files=normal")
    if ok:
        wanted = set(paths or [])
        for line in status.splitlines():
            if not line.startswith("?? "):
                continue
            path = line[3:].strip()
            if wanted and path not in wanted:
                continue
            diffs.setdefault(path, FileDiff(path=path, untracked=True))
    return [diffs[k] for k in sorted(diffs)], ""


def full_diff(repo_path: str, paths: list[str] | None = None) -> str:
    """The patch itself, for `/diff --full`."""
    args = ["diff", "HEAD"]
    if paths:
        args += ["--", *paths]
    ok, out = _git(repo_path, *args, timeout=20.0)
    if not ok:
        ok, out = _git(repo_path, "diff", *(["--", *paths] if paths else []),
                       timeout=20.0)
    return out if ok else ""


# --- /doctor ----------------------------------------------------------------

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


def _manifest_checks(doc: Doctor, slots, reachable: bool) -> None:
    """Fallback-kit checks: manifest resolution, degrade state, weight presence.

    The 2026-07-29 incident this exists for: champion weights silently deleted,
    chat started fine and returned nothing. Nothing owned the assertion "these
    weights should be here" — now this does. Never raises.
    """
    try:
        cfg = slots.cfg
        manifest = cfg.host_manifest()
        if not getattr(cfg, "hosts", None):
            doc.add("host manifest", OK, "none configured (single-model default)")
            return
        if manifest is None:
            from luxe.config import short_hostname
            doc.add("host manifest", WARN,
                    f"no hosts: entry matches {short_hostname() or '(unknown)'!r}",
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

        # Weight presence on the LOCAL store — a remote endpoint's disk is its
        # own doctor's problem; served-catalog coverage is checked below.
        from luxe.chat.origin import endpoint_is_local
        from luxe.modelstore import model_state
        if endpoint_is_local(getattr(slots.backend, "base_url", "")):
            for mid in manifest.all_models():
                state = model_state(mid)
                role = ("main" if mid == manifest.main
                        else "fallback" if mid == manifest.fallback else "keep")
                if state == "ok":
                    doc.add(f"weights:{mid}", OK, f"{role} · on disk")
                else:
                    sev = FAIL if role == "main" else WARN
                    why = ("store entry dangles into a wiped cache"
                           if state == "dangling" else "not in the store")
                    doc.add(f"weights:{mid}", sev, f"{role} · {why}",
                            f"`luxe pull {mid}` (kappa mount or HF)")
        if reachable:
            try:
                served = set(slots.backend.list_models())
            except Exception:
                served = set()
            for mid in (manifest.main, manifest.fallback):
                if mid and served and mid not in served:
                    doc.add(f"served:{mid}", WARN, "not in the server catalog",
                            "restart oMLX after provisioning "
                            "(`brew services restart omlx`)")
    except Exception as e:
        doc.add("host manifest", WARN, f"check errored: {e}")


def run_doctor(session, slots, repo_path: str) -> Doctor:
    """Preflight the things that silently break a chat session.

    Ordered so the first FAIL is the one to fix first: an unreachable endpoint
    makes every later check meaningless.
    """
    from luxe.modelstore import human_bytes

    doc = Doctor()
    backend = slots.backend
    base_url = getattr(backend, "base_url", "") or "?"
    name = getattr(slots, "backend_name", "?")

    reachable = False
    try:
        reachable = bool(backend.health())
    except Exception as e:
        doc.add("oMLX endpoint", FAIL, f"{name} {base_url}: {e}",
                "start it (`brew services start omlx`) or `/backend <other>`")
    else:
        if reachable:
            doc.add("oMLX endpoint", OK, f"{name} {base_url}")
        else:
            doc.add("oMLX endpoint", FAIL, f"{name} {base_url} not responding",
                    "`brew services restart omlx`, or `/backend <other>`")

    if not getattr(backend, "api_key", ""):
        doc.add("API key", WARN, "no key resolved for this endpoint",
                "set OMLX_API_KEY (see ~/.luxe/secrets.env)")
    else:
        doc.add("API key", OK, "present")

    model = slots.model_for("chat")
    if reachable:
        try:
            available = set(slots.available_models())
        except Exception:
            available = set()
        if available and model not in available:
            doc.add("chat model", FAIL, f"{model} is not served here",
                    f"`/model chat <id>`, or `luxe pull {model}`")
        else:
            doc.add("chat model", OK, model)
        org = origin_mod.cached_origin_for(backend, model)
        if org.kind == "unknown":
            try:
                org = origin_mod.origin_for(backend, model)
            except Exception:
                pass
        if org.kind == "network":
            doc.add("weights", WARN, f"{org.glyph} {org.detail}",
                    "loading streams them over the network — expect a slow "
                    "first turn")
        elif org.kind == "remote":
            doc.add("weights", WARN, f"{org.glyph} served by {org.detail}",
                    "prompts and weights cross the network")
        elif org.kind == "local":
            doc.add("weights", OK, f"{org.glyph} local disk")
        else:
            doc.add("weights", WARN, "location unreported by oMLX")
    else:
        doc.add("chat model", WARN, f"{model} (unverified — endpoint down)")

    # Host manifest (fallback kit): the declared main/fallback pair for this
    # machine, and whether its weights are actually on disk. Config-only plus
    # local filesystem — runs in no-project sessions too. Guarded throughout
    # (doctor contract: nothing here may raise).
    _manifest_checks(doc, slots, reachable)

    try:
        free = shutil.disk_usage(Path.home()).free
        if free < _MIN_FREE_GB * 1024**3:
            doc.add("disk", WARN, f"{human_bytes(free)} free",
                    f"a weight swap wants ~{_MIN_FREE_GB} GB of headroom")
        else:
            doc.add("disk", OK, f"{human_bytes(free)} free")
    except OSError as e:
        doc.add("disk", WARN, str(e))

    # Update check — the ONE doctor line allowed to touch the network (a
    # short git fetch on the luxe source repo). Startup banners stay
    # offline-pure; /doctor is exactly where a round-trip is tolerable.
    # Offline is NOT a warning: doctor runs during outages by design.
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

    # Search index: built at startup from the repo HEAD; a moved HEAD means the
    # model is searching a tree that no longer matches the files it will read.
    # A session with no project deliberately has none — that's a state, not a
    # fault, so it reports OK with the escape hatch rather than a warning.
    from luxe import search as search_mod

    index = search_mod.get_index()
    if getattr(session, "project_kind", "git") == "none":
        doc.add("project", OK, f"none attached ({repo_path or 'cwd'})",
                "`/project <path>` or `/index` to enable code search")
        doc.add("search index", OK, "not built (no project)")
        doc.add("mode", OK, f"{'write on' if session.write_enabled else 'read-only'} · "
                            f"bash {'unrestricted' if session.unrestricted_bash else 'allowlisted'}")
        try:
            import textual  # noqa: F401
            doc.add("TUI", OK, "textual installed")
        except ImportError:
            doc.add("TUI", WARN, "textual missing — line REPL fallback",
                    "`uv sync --extra chat`")
        return doc
    if repo_path:
        from luxe.gitkit.health import current_head, is_git_repo

        if index is None:
            doc.add("search index", WARN, "not built",
                    "restart chat in the repo you want indexed")
        else:
            doc.add("search index", OK, f"{len(getattr(index, 'paths', []))} files")
        if is_git_repo(repo_path):
            head = current_head(repo_path)
            indexed = getattr(session, "index_head", "") or ""
            if indexed and head and not head.startswith(indexed) \
                    and not indexed.startswith(head):
                doc.add("index freshness", WARN,
                        f"indexed at {indexed}, HEAD is now {head}",
                        "restart chat to reindex")
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
        else:
            doc.add("git", WARN, "not a git repo",
                    "`/diff` and git tools won't work here")
    else:
        doc.add("repo", WARN, "no repo path for this session")

    mode = ("write on" if session.write_enabled else "read-only")
    bash = ("unrestricted" if session.unrestricted_bash else "allowlisted")
    doc.add("mode", OK, f"{mode} · bash {bash}",
            "" if session.write_enabled else "`/write` to let the model edit files")

    try:
        import textual  # noqa: F401
        doc.add("TUI", OK, "textual installed")
    except ImportError:
        doc.add("TUI", WARN, "textual missing — line REPL fallback",
                "`uv sync --extra chat`")
    return doc
