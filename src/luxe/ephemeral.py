"""Ephemeral mode — a session that leaves nothing behind.

`luxe chat --ephemeral` (and `luxe code --ephemeral`) runs a normal session
with every one of luxe's OWN persistence sites suppressed: no
`~/.luxe/sessions/<id>/` directory, no `meta.json`, `transcript.jsonl`,
`fold.jsonl` or `debug.log`, no `~/.luxe/runs/<run-id>/events.jsonl`, and no
writes to the repo's `.luxe/memory.md` (notes, `/note`, the `/init` brief) or
`facts.jsonl`.

Scope, stated precisely because the name invites a wider reading:

  - It suppresses luxe's BOOKKEEPING, not the model's work. `/write` still
    gates the write tools, and when they are on the agent edits the user's
    project exactly as before. "Ephemeral" is about what luxe records, not
    about what you asked it to do.
  - READS are untouched: config, `~/.luxe/secrets.env`, the theme preference,
    the model store, existing project memory. A session that cannot read its
    own config is not a session.
  - The repo LOCK is deliberately still taken (`~/.luxe/locks/`). It is a
    mutual-exclusion primitive that stops two luxe sessions writing one repo,
    not a record of this session, and dropping it would make `--ephemeral`
    quietly mean "less safe". It is the single artifact that survives, it
    carries pid/run_id/repo_path, and the startup line says so.

Implemented as a process-global rather than a threaded parameter: it is fixed
before anything runs and never changes, and the alternative is a flag through
`append_turn`/`append_event`'s several dozen call sites. `luxe.sdd` forbids
making `luxe_home()` configurable, so redirecting the root at a tmpdir — which
would also capture the read paths above — is not an option.

The benchmark/maintain path never calls `enable()`; `is_ephemeral()` is False
there and every writer behaves exactly as it always has.
"""

from __future__ import annotations

_EPHEMERAL = False


def enable() -> None:
    """Turn ephemeral mode on for this process."""
    global _EPHEMERAL
    _EPHEMERAL = True


def disable() -> None:
    """Turn it back off — subsequent writes land normally.

    Turning it off does NOT recover the turns that were not recorded while it
    was on; the transcript simply has a hole. `/ephemeral` says so.
    """
    global _EPHEMERAL
    _EPHEMERAL = False


def set_enabled(on: bool) -> None:
    enable() if on else disable()


def is_ephemeral() -> bool:
    return _EPHEMERAL


def purge_session(session_id: str, repo_path: str = "") -> list[str]:
    """Delete what this session already wrote. Returns the paths removed.

    `/ephemeral` mid-session has a problem `--ephemeral` does not: the session
    directory already exists, holding a transcript of everything said before
    the toggle. Leaving it is the opposite of what was asked for, so the
    command removes it — but ONLY this session's own directories:

      ~/.luxe/sessions/<id>/     this session's transcript, fold, meta, ledger
      ~/.luxe/runs/<id>-*/       the per-turn telemetry for those turns

    Deliberately NOT touched: `<repo>/.luxe/memory.md`, which mixes
    machine-managed blocks with the user's own curated text — a mode that
    writes nothing must not become a mode that deletes hand-written notes. Any
    note already spliced there stays, and the caller reports it.
    """
    import shutil

    from luxe.memory.session import session_dir
    from luxe.run_state import runs_root

    removed: list[str] = []
    if not session_id:
        return removed

    sd = session_dir(session_id)
    if sd.exists():
        shutil.rmtree(sd, ignore_errors=True)
        if not sd.exists():
            removed.append(str(sd))

    # Run ids are "<session_id>-<n>" (chat/repl.py), so the session's runs are
    # exactly the directories carrying that prefix.
    root = runs_root()
    if root.is_dir():
        for d in sorted(root.glob(f"{session_id}-*")):
            shutil.rmtree(d, ignore_errors=True)
            if not d.exists():
                removed.append(str(d))
    return removed


#: What the mode suppresses and what it does not, stated at startup. Both
#: halves matter: a user who believes `--ephemeral` also disables the write
#: tools would hand it a task that edits their repo, and a user who does not
#: know the debug log is gone will look for it after something goes wrong.
STARTUP_NOTICE = (
    "ephemeral session — no transcript, no debug.log, no run events, no "
    "project-memory writes. Cannot be resumed, and nothing survives for "
    "post-hoc diagnosis. Write tools are unaffected (/write still gates them); "
    "the repo lock is still taken."
)


def startup_notice() -> str:
    """The disclosure line, or "" when the mode is off."""
    return STARTUP_NOTICE if _EPHEMERAL else ""


def _reset_for_tests() -> None:
    """Restore the default. Tests only — there is no runtime path that turns
    ephemeral mode back off."""
    global _EPHEMERAL
    _EPHEMERAL = False
