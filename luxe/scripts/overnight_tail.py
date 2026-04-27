"""Foreground tail for an in-progress overnight run. Prints phase
transitions, current activity, tool-call events, and errors.

Watches three sources concurrently:
- results/overnight_<ts>/state.json — phase status + which phase is "running"
- results/overnight_<ts>/<phase>.log — stdout/stderr of the active phase
- ~/.luxe/tasks/<id>/log.jsonl — per-tool-call events for any running /review

Usage:

    # auto-detect the most recent overnight run
    uv run python scripts/overnight_tail.py

    # follow a specific run
    uv run python scripts/overnight_tail.py --dir results/overnight_2026-04-25T07-17-02

    # tighter heartbeat (default 30s)
    uv run python scripts/overnight_tail.py --heartbeat-s 10

Ctrl-C to exit; the underlying overnight process keeps running.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parent.parent
TASKS_ROOT = Path.home() / ".luxe" / "tasks"


# ── ANSI helpers (no rich dep — keep this script standalone) ────────


def _color(s: str, code: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


_DIM = lambda s: _color(s, "2")
_BOLD = lambda s: _color(s, "1")
_GREEN = lambda s: _color(s, "32")
_YELLOW = lambda s: _color(s, "33")
_RED = lambda s: _color(s, "31")
_CYAN = lambda s: _color(s, "36")


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _emit(prefix: str, msg: str) -> None:
    print(f"{_DIM(_ts())} {prefix} {msg}", flush=True)


# ── source: state.json ───────────────────────────────────────────────


def _read_state(out_dir: Path) -> dict:
    p = out_dir / "state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def _phase_label(name: str, info: dict) -> str:
    status = info.get("status", "?")
    wall = info.get("wall_s")
    color = {
        "done": _GREEN, "running": _CYAN, "failed": _RED,
        "timeout": _RED, "skipped_dry_run": _DIM, "skipped_by_user": _DIM,
    }.get(status, lambda s: s)
    label = f"{name}={color(status)}"
    if wall:
        label += _DIM(f" ({wall:.0f}s)")
    return label


# ── source: phase log file ───────────────────────────────────────────


def _open_for_tail(p: Path, rewind_bytes: int = 4096):
    """Open file; return a file handle positioned `rewind_bytes` from
    EOF (so user sees the last N bytes of context on attach, not just
    new appends). Returns None if path doesn't exist yet."""
    if not p.exists():
        return None
    f = p.open("r", encoding="utf-8", errors="replace")
    size = p.stat().st_size
    f.seek(max(0, size - rewind_bytes), os.SEEK_SET)
    if size > rewind_bytes:
        # Discard partial first line so we don't emit a chopped fragment.
        f.readline()
    return f


def _drain(handle, prefix: str, ignore_blank: bool = True) -> int:
    """Read any newly-appended lines from `handle` and emit them.
    Returns number of lines emitted. Caller is responsible for keeping
    the handle alive across calls."""
    if handle is None:
        return 0
    n = 0
    while True:
        line = handle.readline()
        if not line:
            return n
        line = line.rstrip("\n")
        if ignore_blank and not line.strip():
            continue
        # rich's progress bars emit a lot of \r-overwrites — when piped
        # to a file you get `\r...new...` lines. Strip the leading \r.
        line = line.lstrip("\r")
        _emit(prefix, line)
        n += 1


# ── source: ~/.luxe/tasks/<id>/log.jsonl ─────────────────────────────


def _find_running_review_tasks(state: dict) -> list[str]:
    """If we're inside Phase 3 (multi_turn_reviews) and the orchestrator
    has spawned background /review processes, find their task ids by
    looking at ~/.luxe/tasks/ for any state.json with status='running'
    that was created since this overnight started."""
    overnight_started = state.get("started_at", "")
    if not overnight_started or not TASKS_ROOT.exists():
        return []
    out: list[str] = []
    for d in TASKS_ROOT.iterdir():
        if not d.is_dir():
            continue
        sp = d / "state.json"
        if not sp.exists():
            continue
        try:
            s = json.loads(sp.read_text())
            if s.get("status") == "running" and s.get("created_at", "") >= overnight_started:
                out.append(d.name)
        except (json.JSONDecodeError, OSError):
            continue
    return sorted(out)


def _emit_review_event(task_id: str, ev: dict) -> None:
    """Pretty-print one log.jsonl event. Mirrors `/tasks tail -v`'s
    one-line-per-event shape but tagged with the task id so we can
    follow multiple concurrent runs."""
    kind = ev.get("event", "?")
    sub = (ev.get("subtask", "") or "").rsplit(".", 1)[-1] or "--"
    short_id = task_id.rsplit("-", 1)[-1]   # last segment is enough to disambiguate
    tag = _CYAN(f"[{short_id}/sub{sub}]")

    if kind == "begin":
        title = (ev.get("title") or "")[:60]
        agent = ev.get("agent", "")
        model = ev.get("model", "")
        _emit(tag, f"{_BOLD('begin')} {agent}/{model} · {title}")
    elif kind == "end":
        wall = ev.get("wall_s", 0)
        tools = ev.get("tool_calls", 0)
        prompt = ev.get("prompt_tokens", 0)
        compl = ev.get("completion_tokens", 0)
        st = ev.get("status", "?")
        col = _GREEN if st == "done" else _RED
        _emit(tag, f"{col('end')} status={st} wall={wall:.0f}s tools={tools} prompt={prompt} compl={compl}")
    elif kind == "tool_call_begin":
        name = ev.get("name", "?")
        _emit(tag, f"{_DIM('  →')} {name}")
    elif kind == "tool_call_end":
        name = ev.get("name", "?")
        wall = ev.get("wall_s", 0)
        ok = ev.get("ok", True)
        bytes_out = ev.get("bytes_out", 0)
        col = _DIM if ok else _RED
        _emit(tag, f"{col(f'  ← {name}')} wall={wall:.2f}s bytes={bytes_out}")
    elif kind == "skip":
        reason = ev.get("reason", "")
        _emit(tag, f"{_YELLOW('skip')} {reason}")
    elif kind == "finish":
        st = ev.get("status", "?")
        col = _GREEN if st == "done" else _YELLOW
        _emit(tag, f"{col(f'finish status={st}')} cache_hits={ev.get('cache_hits', 0)} cache_misses={ev.get('cache_misses', 0)}")
    else:
        _emit(tag, f"{_DIM(kind)} {json.dumps({k: v for k, v in ev.items() if k not in ('ts', 'event', 'subtask')})[:140]}")


# ── orchestrator ────────────────────────────────────────────────────


def _resolve_out_dir(arg: str) -> Path:
    """Either explicit dir or auto-pick the most recent overnight_*."""
    if arg:
        p = Path(arg)
        if not p.is_absolute():
            p = ROOT / p
        if not p.exists():
            print(f"--dir not found: {p}", file=sys.stderr)
            sys.exit(2)
        return p
    candidates = sorted((ROOT / "results").glob("overnight_*"))
    if not candidates:
        print("no overnight_* directories found in results/", file=sys.stderr)
        sys.exit(2)
    return candidates[-1]


def main(
    dir: str = typer.Option("", "--dir",
        help="Path to a specific results/overnight_<ts>/ dir. Default: most recent."),
    heartbeat_s: int = typer.Option(30, "--heartbeat-s",
        help="Print a 'still alive, no new events' line at this cadence."),
    poll_s: float = typer.Option(1.5, "--poll-s",
        help="How often to check sources for new content."),
) -> None:
    out_dir = _resolve_out_dir(dir)
    print(_BOLD(f"watching {out_dir}"), file=sys.stderr)
    print(_DIM("Ctrl-C to stop watching (overnight process unaffected)"), file=sys.stderr)
    print("", file=sys.stderr)

    last_phase_status: dict[str, str] = {}
    last_emit = time.monotonic()
    phase_log_handle = None
    phase_log_path: Path | None = None
    review_log_handles: dict[str, object] = {}  # task_id → file handle

    def _close_phase_log():
        nonlocal phase_log_handle, phase_log_path
        if phase_log_handle is not None:
            try: phase_log_handle.close()
            except Exception: pass
        phase_log_handle = None
        phase_log_path = None

    try:
        while True:
            state = _read_state(out_dir)
            now = time.monotonic()
            had_output = False

            # 1. Phase transitions
            for name, info in state.get("phases", {}).items():
                st = info.get("status", "?")
                if last_phase_status.get(name) != st:
                    _emit(_BOLD("[phase]"), _phase_label(name, info))
                    last_phase_status[name] = st
                    had_output = True
                    # When a phase enters "running", switch the
                    # phase-log tail to its log file.
                    if st == "running":
                        new_log = out_dir / f"{name}.log"
                        if new_log != phase_log_path:
                            _close_phase_log()
                            phase_log_handle = _open_for_tail(new_log)
                            phase_log_path = new_log if phase_log_handle else None
                            if phase_log_handle is None:
                                _emit(_DIM("[tail]"),
                                      f"waiting for {new_log.name} to appear...")

            # If the active phase's log file appeared after the
            # transition, attach lazily on first poll where it exists.
            if phase_log_handle is None and phase_log_path is not None:
                phase_log_handle = _open_for_tail(phase_log_path)
                if phase_log_handle is not None:
                    _emit(_DIM("[tail]"), f"attached to {phase_log_path.name}")
            # Or if no current phase but state has any "running" phase
            # missed (script started mid-phase), attach to it now.
            if phase_log_handle is None:
                running = [(n, i) for n, i in state.get("phases", {}).items()
                           if i.get("status") == "running"]
                if running:
                    name = running[0][0]
                    cand = out_dir / f"{name}.log"
                    phase_log_handle = _open_for_tail(cand)
                    if phase_log_handle is not None:
                        phase_log_path = cand
                        _emit(_DIM("[tail]"), f"attached to {cand.name} (mid-phase)")

            # 2. Drain phase log
            n = _drain(phase_log_handle, _DIM(f"[{phase_log_path.stem if phase_log_path else 'phase'}]"))
            had_output |= bool(n)

            # 3. Drain ~/.luxe/tasks/<id>/log.jsonl for any running review tasks
            running_reviews = set(_find_running_review_tasks(state))
            # Open new ones
            for tid in running_reviews:
                if tid not in review_log_handles:
                    log_p = TASKS_ROOT / tid / "log.jsonl"
                    h = _open_for_tail(log_p)
                    if h is not None:
                        review_log_handles[tid] = h
                        _emit(_CYAN("[review]"), f"following {tid}")
                        had_output = True
            # Close finished ones
            for tid in list(review_log_handles):
                if tid not in running_reviews:
                    try: review_log_handles[tid].close()
                    except Exception: pass
                    del review_log_handles[tid]
                    _emit(_CYAN("[review]"), f"stopped following {tid}")
                    had_output = True
            # Drain each
            for tid, h in review_log_handles.items():
                while True:
                    line = h.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    _emit_review_event(tid, ev)
                    had_output = True

            # 4. Heartbeat if quiet
            if had_output:
                last_emit = now
            elif (now - last_emit) >= heartbeat_s:
                # Compose a one-line status: which phase is running, elapsed
                running = [(n, i) for n, i in state.get("phases", {}).items()
                           if i.get("status") == "running"]
                if running:
                    name, info = running[0]
                    started = info.get("started_at", "?")
                    _emit(_DIM("[heartbeat]"),
                          f"{name} running since {started} — {len(running_reviews)} review(s) in flight")
                elif state.get("finished_at"):
                    _emit(_BOLD("[done]"),
                          f"overnight finished at {state['finished_at']}")
                    return
                else:
                    _emit(_DIM("[heartbeat]"), "idle (between phases)")
                last_emit = now

            # 5. Detect terminal completion
            if state.get("finished_at"):
                # Drain any final phase log content
                _drain(phase_log_handle, _DIM("[phase]"))
                _emit(_BOLD("[done]"),
                      f"overnight finished at {state['finished_at']} — exiting")
                return

            time.sleep(poll_s)
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        _emit(_BOLD("[tail]"), "stopped (overnight process unaffected)")
    finally:
        _close_phase_log()
        for h in review_log_handles.values():
            try: h.close()
            except Exception: pass


if __name__ == "__main__":
    typer.run(main)
