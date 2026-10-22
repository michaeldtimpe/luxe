"""Session introspection for `luxe chat`: `/export`, `/diff`, `/doctor`.

The logic lives here as pure-ish functions so it can be unit-tested without a
console, a model, or a live oMLX: `commands.py` keeps only the rendering. All
three are read-only — nothing here mutates a repo or a session.

`/export` lives here; `/diff` is `chat/gitdiff.py` and `/doctor` is
`chat/doctor.py` (split out 2026-10), both re-exported below so
`inspection.run_doctor` / `inspection.render_doctor` (the renderer `luxe
ready` shares) / `inspection.session_diff` keep resolving. A re-export is a
copied binding: code that stubs a doctor lookup must patch `chat.doctor`.
"""

from __future__ import annotations

import time
from pathlib import Path

from luxe.chat.doctor import (  # noqa: F401  (re-export: the /doctor surface)
    FAIL,
    OK,
    WARN,
    Check,
    Doctor,
    _add_stale_check,
    _fetch_catalog,
    _LIVENESS_TIMEOUT_S,
    _weights_fix,
    backend_entry_for,
    endpoint_fixes,
    hostwide_view,
    render_doctor,
    run_doctor,
    stale_build_check,
    weights_checks,
)
from luxe.chat.gitdiff import (  # noqa: F401  (re-export: the /diff surface)
    FileDiff,
    full_diff,
    session_diff,
)
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
