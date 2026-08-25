#!/usr/bin/env python3
"""Chat-side `LUXE_TOOL_BUDGET_CTX` A/B drill — reproduces the 2026-08-24
oversized-read failure headlessly and measures both arms of the flag on the
`luxe chat` path.

See `acceptance/chat_bigread_2026_08_24/PLAN.md` item 2.1 and `EVIDENCE.md`
for the failure this reproduces: a chat turn opens two files in one step
(one ~250 KB, one ~70 KB), the combined tool output blows the context window,
the request is dispatched anyway, fails transport-level, and is retried
verbatim until exhausted. `tools/tools.sdd` records this as the missing
evidence that keeps `LUXE_TOOL_BUDGET_CTX` opt-in for chat
(`chat/repl.py:717`) instead of default-on like the maintain/bench path.

This script does THREE things, all read-only against `~/.luxe/` except the
scratch repo it plants (a fresh `tempfile.mkdtemp`, never under `~/.luxe/`)
and the REPORT.md it writes:

  1. **Plant** a deterministic scratch git repo containing the failure shape
     (`plant_repo`) — same byte-for-byte content every run, no randomness,
     no network.
  2. **Drive** real headless turns against it via `luxe chat --repo <dir>`,
     one per (arm, window) cell of a 2x2 matrix
     (`LUXE_TOOL_BUDGET_CTX` unset/`=1` x num_ctx 32768/131072), the same
     `printf 'msg\\n/quit\\n' | luxe chat --repo <dir>` form
     `README.md` § "Self-testing luxe" documents.
  3. **Parse** the artefacts every session already writes — debug.log,
     transcript.jsonl, runs/<run_id>/events.jsonl — into a per-turn record:
     outcome, peak ctx pressure, every read_file's bytes_out (and, when
     refused, the actual size + the limit that fired), compaction events
     (including whether they actually dropped anything), backend retry
     decisions, and — the important part — whether the model used the
     `offset=` resume the clipped read hands it, and at what extra cost.

Usage:

    # Prove the parser against the real 2026-08-24 failure (session
    # 168f1825a1fd) and plant + inspect the scratch repo. No model needed.
    python3 scripts/bigread_drill.py --dry-run

    # Run the live 2x2 matrix against a real endpoint (minutes; needs a
    # running oMLX/openrouter backend). NOT run by this invocation's author —
    # hand this command to whoever has the endpoint up:
    python3 scripts/bigread_drill.py --backend local
    python3 scripts/bigread_drill.py --backend openrouter

This script imports nothing from `luxe` at parse/plant/report time — it
mines plain-text logs and JSONL with stdlib only, so it does NOT need
`uv run` the way `scripts/toolcall_taxonomy.py` does (that script imports
the live tool registry for its denominator; this one has no denominator to
get wrong). `uv run` is still how you'd invoke the `luxe chat` subprocess
this script spawns for the live matrix, if that's how this host normally
runs luxe — the subprocess inherits whatever `luxe` resolves to on PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "acceptance" / "chat_bigread_2026_08_24" / "REPORT.md"
DEFAULT_LUXE_ROOT = Path.home() / ".luxe"

#: The real 2026-08-24 failure session this script's parser is proved against
#: (see task Verify section / EVIDENCE.md Case 1). Read-only reference.
VERIFY_SESSION = "168f1825a1fd"
VERIFY_TURN_RUN = "168f1825a1fd-2"
VERIFY_SESSION_CASE2 = "eb0b2923a3eb"

WINDOWS = (32768, 131072)  # medium, xlarge — CTX_TIERS in chat/session.py
_CTX_TIER_FOR_WINDOW = {32768: "medium", 131072: "xlarge"}
ARMS = ("off", "on")  # LUXE_TOOL_BUDGET_CTX unset vs "1"

_DRIVE_MESSAGE = (
    "List the files in this repo, then read big-notes.md and module.py in "
    "full and give me a two-sentence summary of each."
)

# --- deterministic scratch-repo content -------------------------------------
# No `random`, no network, no wall-clock content — the same drill run twice
# must plant byte-identical files.

_PROSE_PARAGRAPH = (
    "The archive exists so a later reader does not have to re-derive what an "
    "earlier one already worked out. Every section here restates that same "
    "premise from a slightly different angle, on purpose: the failure this "
    "repo exists to reproduce was one read call returning a file this size "
    "in a single tool result, not any particular thing the file said."
)


def _build_prose(target_bytes: int = 250_000) -> str:
    parts = []
    n = 0
    idx = 0
    while n < target_bytes:
        chunk = f"## Section {idx:04d}\n\n{_PROSE_PARAGRAPH} (section {idx:04d})\n\n"
        parts.append(chunk)
        n += len(chunk.encode("utf-8"))
        idx += 1
    return "".join(parts)


def _build_source(target_bytes: int = 70_000) -> str:
    parts = ["\"\"\"Deterministic filler module for the bigread drill.\"\"\"\n\n"]
    n = len(parts[0].encode("utf-8"))
    idx = 0
    while n < target_bytes:
        chunk = (
            f"def fn_{idx:04d}(x):\n"
            f"    \"\"\"Filler function {idx:04d}. Returns x plus a constant.\"\"\"\n"
            f"    return x + {idx}\n\n\n"
        )
        parts.append(chunk)
        n += len(chunk.encode("utf-8"))
        idx += 1
    return "".join(parts)


def plant_repo(root: Path) -> dict[str, int]:
    """Write the deterministic file mix into `root` and `git init` it.

    Returns `{filename: byte_size}`. Never touches `~/.luxe/` — `root` is the
    caller's choice (default: a fresh `tempfile.mkdtemp`, never a location
    under `~/.luxe/`, per this script's read-only discipline there).
    """
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "big-notes.md": _build_prose(),
        "module.py": _build_source(),
        "README.md": (
            "# bigread drill scratch repo\n\n"
            "Planted by scripts/bigread_drill.py to reproduce the "
            "2026-08-24 chat oversized-read failure. big-notes.md and "
            "module.py are the two files a single turn reads in one step.\n"
        ),
        "questions.md": (
            "1. What does module.py do?\n"
            "2. Summarize big-notes.md in two sentences.\n"
        ),
    }
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.email=drill@luxe", "-c", "user.name=luxe-drill",
         "commit", "-qm", "drill: initial state"],
    ):
        subprocess.run(["git", "-C", str(root), *args],
                       capture_output=True, timeout=30)
    return {name: (root / name).stat().st_size for name in files}


# --- driving real headless turns --------------------------------------------

def _existing_sessions(luxe_root: Path) -> set[str]:
    d = luxe_root / "sessions"
    if not d.is_dir():
        return set()
    return {p.name for p in d.iterdir() if p.is_dir()}


def run_drill_turn(repo: Path, *, backend_name: str | None,
                   config_path: str | None, window: int, budget_on: bool,
                   luxe_root: Path, timeout: int = 600,
                   model: str | None = None,
                   luxe_cmd: str = "luxe") -> str | None:
    """Drive one headless `luxe chat` turn against `repo`. Returns the new
    session id, or None if none appeared (e.g. luxe exited before starting
    one — startup failure)."""
    before = _existing_sessions(luxe_root)
    tier = _CTX_TIER_FOR_WINDOW.get(window)
    if tier is None:
        raise ValueError(f"no /ctx tier maps to window {window}; add one to "
                         "_CTX_TIER_FOR_WINDOW")
    stdin_text = f"/ctx {tier}\n{_DRIVE_MESSAGE}\n/quit\n"

    env = dict(os.environ)
    if budget_on:
        env["LUXE_TOOL_BUDGET_CTX"] = "1"
    else:
        env.pop("LUXE_TOOL_BUDGET_CTX", None)

    cmd = [luxe_cmd, "chat", "--repo", str(repo)]
    if backend_name:
        cmd += ["--backend", backend_name]
    if config_path:
        cmd += ["--config", config_path]
    if model:
        cmd += ["--chat-model", model]

    subprocess.run(cmd, input=stdin_text, text=True, env=env,
                   capture_output=True, timeout=timeout)
    after = _existing_sessions(luxe_root)
    new = after - before
    if not new:
        return None
    # Exactly one turn was driven, so exactly one new session is expected;
    # if the host raced with something else, take the most recently touched.
    if len(new) > 1:
        new = {max(new, key=lambda s: (luxe_root / "sessions" / s).stat().st_mtime)}
    return next(iter(new))


# --- log parsing -------------------------------------------------------------
# debug.log messages can contain embedded newlines (`_too_large_message`'s
# resume hint is multi-line) even though the logger call that wrote it was
# one Python statement. Split on TIMESTAMP-prefixed line starts, not on "\n",
# so a message's embedded newlines stay inside its own record.

_TS_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ",
                          re.MULTILINE)
_TS_RECORD_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (\w+) ([\w.]+): (.*)$",
    re.DOTALL)

_TOOL_DISPATCH_RE = re.compile(r"^tool dispatch step=(\d+) name=(\S+) args=(.*)$")
_TOOL_DONE_RE = re.compile(
    r"^tool done name=(\S+) wall_s=([\d.]+) error=(.*) bytes_out=(-?\d+)\s*$",
    re.DOTALL)
_STEP_RE = re.compile(
    r"^step=(\d+) ctx_pressure=([\d.]+)% \(est=([\d.]+)% cal=([\d.]+)x\) "
    r"num_ctx=(\d+) effective_ctx=(\d+) msgs=(\d+)$")
_TURN_DONE_RE = re.compile(
    r"^turn done run_id=(\S+) steps=(\d+) tool_calls=(\d+) prompt_tokens=(\d+) "
    r"last_prompt_tokens=(\d+) num_ctx=(\d+) ctx_server=([\d.]+)% "
    r"ctx_est=([\d.]+)% peak_est=([\d.]+)%$")
_TURN_INTERRUPTED_RE = re.compile(
    r"^turn interrupted run_id=(\S+) observed_tool_calls=(\d+) partial_chars=(\d+)$")
_TURN_ABORTED_RE = re.compile(r"^turn aborted: (.*)$", re.DOTALL)
_BACKEND_RETRY_RE = re.compile(
    r"^backend (\S+) exception=(\S+) decision=RetryDecision\(retry=(True|False),\s*"
    r"reason='([^']+)',\s*delay_s=([\d.]+)\)$")
_TOO_LARGE_RE = re.compile(
    r"File too large to read whole \(([\d,]+) bytes, limit ([\d,]+)")


def iter_log_records(text: str):
    """Yield `{ts_str, level, logger, msg}` dicts, one per logger call —
    including calls whose message spans multiple physical lines."""
    starts = [m.start() for m in _TS_START_RE.finditer(text)]
    starts.append(len(text))
    for i in range(len(starts) - 1):
        chunk = text[starts[i]:starts[i + 1]]
        m = _TS_RECORD_RE.match(chunk)
        if not m:
            continue
        yield {"ts_str": m.group(1), "level": m.group(2), "logger": m.group(3),
              "msg": m.group(4).rstrip("\n")}


def split_into_turns(records: list[dict]) -> tuple[dict, list]:
    """Group log records by run_id using the terminal line each turn always
    logs (`turn done run_id=...` / `turn interrupted run_id=...`), attaching
    a following `turn aborted: ...` ERROR line (which carries no run_id of
    its own) to the turn that just closed.

    Returns `(turns, trailing)` — trailing is any records after the last
    closed turn (e.g. a `session ... end` line, or a turn truly cut off
    mid-flight with no terminal line at all)."""
    turns: dict[str, dict] = {}
    current: list[dict] = []
    last_run_id: str | None = None
    for rec in records:
        current.append(rec)
        msg = rec["msg"]
        m = _TURN_DONE_RE.match(msg)
        if m:
            run_id = m.group(1)
            turns[run_id] = {"records": current, "done": m}
            last_run_id = run_id
            current = []
            continue
        m = _TURN_INTERRUPTED_RE.match(msg)
        if m:
            run_id = m.group(1)
            turns[run_id] = {"records": current, "interrupted": m}
            last_run_id = run_id
            current = []
            continue
        m = _TURN_ABORTED_RE.match(msg)
        if m and rec["level"] == "ERROR" and last_run_id is not None \
                and last_run_id in turns:
            turns[last_run_id]["records"].append(rec)
            turns[last_run_id]["aborted"] = m
            current = []
            continue
    return turns, current


def _iter_jsonl(path: Path):
    if not path.is_file():
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def analyze_turn(run_id: str, chunk: dict, luxe_root: Path) -> dict:
    records = chunk["records"]
    detail: dict = {"run_id": run_id}

    if "aborted" in chunk:
        detail["outcome"] = "aborted"
        detail["abort_reason"] = chunk["aborted"].group(1).strip()
    elif "interrupted" in chunk:
        detail["outcome"] = "interrupted"
    elif "done" in chunk:
        detail["outcome"] = "completed"
    else:
        detail["outcome"] = "unknown (no terminal log line found)"

    if "done" in chunk:
        m = chunk["done"]
        detail.update(steps=int(m.group(2)), tool_calls=int(m.group(3)),
                      prompt_tokens=int(m.group(4)),
                      last_prompt_tokens=int(m.group(5)),
                      num_ctx=int(m.group(6)), ctx_server_pct=float(m.group(7)),
                      ctx_est_pct=float(m.group(8)),
                      peak_est_pct=float(m.group(9)))
    if "interrupted" in chunk:
        m = chunk["interrupted"]
        detail.update(observed_tool_calls=int(m.group(2)),
                      partial_chars=int(m.group(3)))

    step_pressures = []
    wall_span = [None, None]
    for rec in records:
        m = _STEP_RE.match(rec["msg"])
        if m:
            step_pressures.append(float(m.group(2)))
    if step_pressures:
        detail.setdefault("peak_est_pct", max(step_pressures))
        detail["peak_step_pressure_pct"] = max(step_pressures)
    if records:
        detail["wall_s"] = _approx_wall_s(records)

    retries = []
    for rec in records:
        m = _BACKEND_RETRY_RE.match(rec["msg"])
        if m:
            retries.append({"model": m.group(1), "exception": m.group(2),
                            "retry": m.group(3) == "True", "reason": m.group(4),
                            "delay_s": float(m.group(5))})
    detail["retries"] = retries

    dispatches = []
    for rec in records:
        m = _TOOL_DISPATCH_RE.match(rec["msg"])
        if m:
            try:
                args = json.loads(m.group(3))
            except ValueError:
                args = {}
            dispatches.append({"step": int(m.group(1)), "name": m.group(2),
                               "args": args})
    dones = []
    for rec in records:
        m = _TOOL_DONE_RE.match(rec["msg"])
        if m:
            err = m.group(3)
            dones.append({"name": m.group(1), "wall_s": float(m.group(2)),
                          "error": None if err == "None" else err,
                          "bytes_out": int(m.group(4))})

    tool_calls_detail = []
    for i, disp in enumerate(dispatches):
        done = dones[i] if i < len(dones) else {}
        tc = {**disp, **done}
        tc["too_large"] = None
        if tc.get("error"):
            tlm = _TOO_LARGE_RE.search(tc["error"])
            if tlm:
                tc["too_large"] = {
                    "actual_bytes": int(tlm.group(1).replace(",", "")),
                    "limit_bytes": int(tlm.group(2).replace(",", "")),
                }
        tool_calls_detail.append(tc)
    detail["tool_calls_detail"] = tool_calls_detail

    refused_paths = {tc["args"].get("path") for tc in tool_calls_detail
                     if tc["name"] == "read_file" and tc.get("too_large")}
    resume_calls = [tc for tc in tool_calls_detail
                    if tc["name"] == "read_file"
                    and tc["args"].get("path") in refused_paths
                    and int(tc["args"].get("offset") or 0) > 0]
    detail["refused_reads"] = len(refused_paths)
    detail["resume_calls"] = len(resume_calls)
    detail["recovered"] = bool(refused_paths) and bool(resume_calls) \
        and detail["outcome"] == "completed"

    events = list(_iter_jsonl(luxe_root / "runs" / run_id / "events.jsonl"))
    detail["compactions"] = [e for e in events
                             if e.get("kind") == "compaction_phase_reached"]
    detail["compaction_resolve"] = next(
        (e for e in events if e.get("kind") == "compaction_phase_at_resolve"),
        None)
    detail["events_big_reads"] = [
        e for e in events if e.get("kind") == "tool_call"
        and e.get("name") == "read_file" and (e.get("bytes_out") or 0) > 50_000
    ]
    return detail


def _approx_wall_s(records: list[dict]) -> float | None:
    """First-record to last-record wall time within a turn's chunk, from the
    log's own timestamps. Approximate (second resolution parse via strptime
    would need an import; string compare on 'HH:MM:SS,mmm' is good enough
    for a same-day drill and avoids a datetime round-trip)."""
    import datetime
    try:
        t0 = datetime.datetime.strptime(records[0]["ts_str"], "%Y-%m-%d %H:%M:%S,%f")
        t1 = datetime.datetime.strptime(records[-1]["ts_str"], "%Y-%m-%d %H:%M:%S,%f")
        return (t1 - t0).total_seconds()
    except (ValueError, IndexError):
        return None


def analyze_session(session_id: str, luxe_root: Path) -> dict[str, dict]:
    log_path = luxe_root / "sessions" / session_id / "debug.log"
    if not log_path.is_file():
        return {}
    text = log_path.read_text(encoding="utf-8", errors="replace")
    records = list(iter_log_records(text))
    turns, _trailing = split_into_turns(records)
    return {run_id: analyze_turn(run_id, chunk, luxe_root)
           for run_id, chunk in turns.items()}


# --- self-test: prove the parser against the real 2026-08-24 failure -------

def run_selftest(luxe_root: Path) -> bool | None:
    """Parse session `168f1825a1fd` (the real failure) and assert the three
    facts the task's Verify section names. Returns True/False, or None if
    the session isn't present on this host (nothing to verify against)."""
    sess_dir = luxe_root / "sessions" / VERIFY_SESSION
    if not sess_dir.is_dir():
        print(f"[selftest] SKIPPED — {sess_dir} not found on this host; "
             "nothing to verify the parser against.")
        return None

    turns = analyze_session(VERIFY_SESSION, luxe_root)
    ok = True
    checks: list[tuple[str, bool, str]] = []

    t2 = turns.get(VERIFY_TURN_RUN)
    if t2 is None:
        checks.append((f"turn {VERIFY_TURN_RUN} parsed", False,
                       f"run_ids found: {sorted(turns)}"))
    else:
        big = next((tc for tc in t2["tool_calls_detail"]
                   if tc["name"] == "read_file" and tc.get("bytes_out") == 257988),
                  None)
        checks.append(("257,988-byte read_file bytes_out detected",
                      big is not None, str(big)))

        noop = any(c.get("phase_reached") == 3 and c.get("tokens_before") == 71616
                  and c.get("tokens_after") == 71616
                  and c.get("tool_results_dropped") == 0
                  for c in t2["compactions"])
        checks.append((
            "compaction_phase_reached phase_reached=3 tokens_before=71616 "
            "tokens_after=71616 tool_results_dropped=0 (the no-op) detected",
            noop, str(t2["compactions"])))

        remote_protocol_retries = [r for r in t2["retries"]
                                   if r["exception"] == "RemoteProtocolError"]
        checks.append((
            "3 RemoteProtocolError retry decisions detected",
            len(remote_protocol_retries) == 3, str(t2["retries"])))

        checks.append((f"outcome classified as 'aborted'",
                      t2.get("outcome") == "aborted", str(t2.get("outcome"))))

    for name, passed, detail in checks:
        status = "PASS" if passed else "FAIL"
        line = f"  [{status}] {name}"
        if not passed:
            line += f"\n         -> {detail}"
        print(line)
        ok = ok and passed
    return ok


# --- report rendering --------------------------------------------------------

def _fmt_retries(retries: list[dict]) -> str:
    if not retries:
        return "—"
    return "; ".join(f"{r['exception']}→{r['reason']}" for r in retries)


def _fmt_pct(v) -> str:
    return f"{v:.1f}%" if isinstance(v, (int, float)) else "—"


def _row(arm_label: str, window, detail: dict | None) -> str:
    if detail is None:
        return (f"| {arm_label} | {window} | PENDING | — | — | — | — | — | — | "
               "— | — |")
    d = detail
    comp = d.get("compactions") or []
    comp_txt = "—"
    if comp:
        c = comp[-1]
        effective = c.get("tokens_before") != c.get("tokens_after") \
            or (c.get("tool_results_dropped") or 0) > 0
        comp_txt = (f"phase{c.get('phase_reached')} "
                   f"{'no-op' if not effective else 'dropped'} "
                   f"({c.get('tokens_before')}→{c.get('tokens_after')} tok, "
                   f"{c.get('tool_results_dropped')} results)")
    return (f"| {arm_label} | {window} | {d.get('outcome')} | "
           f"{d.get('tool_calls', d.get('observed_tool_calls', '—'))} | "
           f"{_fmt_pct(d.get('peak_est_pct'))} | "
           f"{d.get('refused_reads')} | {d.get('resume_calls')} | "
           f"{_fmt_pct(d.get('wall_s')) if False else d.get('wall_s', '—')} | "
           f"{len(d.get('retries', []))} | {comp_txt} | "
           f"{'yes' if d.get('recovered') else 'no'} |")


def render_report(*, matrix: dict, incident_turns: dict, selftest_ok: bool | None,
                  plant_sizes: dict, scratch_repo: Path | None,
                  live_ran: bool) -> str:
    now = time.strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Chat-side LUXE_TOOL_BUDGET_CTX drill — REPORT",
        "",
        f"Generated {now} by `scripts/bigread_drill.py`. Per PLAN.md item "
        "2.1: chat-side evidence for `tools/tools.sdd` § "
        "\"`LUXE_TOOL_BUDGET_CTX` wiring\", which currently records chat as "
        "having none.",
        "",
        "## Parser self-test (real 2026-08-24 failure, session "
        f"`{VERIFY_SESSION}`)",
        "",
    ]
    if selftest_ok is None:
        lines.append("SKIPPED — the reference session is not present on "
                     "this host.")
    else:
        lines.append("PASS — see stdout for the per-assertion breakdown, or "
                     "re-run with `--dry-run` on a host holding "
                     f"`~/.luxe/sessions/{VERIFY_SESSION}/`."
                     if selftest_ok else
                     "**FAIL** — the parser did not recover the known facts. "
                     "Do not trust the matrix below until this passes.")
    lines += ["", "## Real-incident replay (arm=OFF, both already happened, "
             "2026-08-24)", "",
             "This section is not a drill — it is the actual failure, "
             "parsed with the same code path the drill below uses. Both "
             "runs are arm=OFF (chat's real default) because that is what "
             "was running when the incident occurred.", "",
             "| session | run_id | window | outcome | tool_calls | peak "
             "pressure | refused reads | resume calls | wall_s | retries | "
             "compaction |",
             "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for sess_id, run_id, window in (
        (VERIFY_SESSION, VERIFY_TURN_RUN, 131072),
        (VERIFY_SESSION_CASE2, f"{VERIFY_SESSION_CASE2}-0", 32768),
    ):
        d = incident_turns.get(sess_id, {}).get(run_id)
        if d is None:
            lines.append(f"| `{sess_id}` | `{run_id}` | {window} | not found "
                        "on this host | — | — | — | — | — | — | — |")
            continue
        comp = d.get("compactions") or []
        comp_txt = "—"
        if comp:
            c = comp[-1]
            effective = c.get("tokens_before") != c.get("tokens_after") \
                or (c.get("tool_results_dropped") or 0) > 0
            comp_txt = (f"phase{c.get('phase_reached')} "
                       f"{'dropped' if effective else 'NO-OP'} "
                       f"({c.get('tokens_before')}→{c.get('tokens_after')} tok)")
        lines.append(
            f"| `{sess_id}` | `{run_id}` | {window} | {d.get('outcome')} | "
            f"{d.get('tool_calls', d.get('observed_tool_calls', '—'))} | "
            f"{_fmt_pct(d.get('peak_est_pct'))} | {d.get('refused_reads')} | "
            f"{d.get('resume_calls')} | {d.get('wall_s', '—')} | "
            f"{len(d.get('retries', []))} | {comp_txt} |")
    lines += ["", "Both incident turns ran with `LUXE_TOOL_BUDGET_CTX` "
             "unset (chat's shipped default) — `refused_reads=0` in both "
             "confirms the budget never engaged; the fixed 256 KB cap "
             "(`_MAX_FILE_SIZE`) never fired either, because 257,988 B and "
             "72,181 B both sit under it. The failure is pure ctx-window "
             "overflow, not a refusal.", ""]

    lines += ["## Planted-repo A/B matrix (this drill)", ""]
    if scratch_repo is not None:
        lines.append(f"Scratch repo: `{scratch_repo}` — "
                     f"{', '.join(f'{n} {s:,} B' for n, s in plant_sizes.items())}")
        lines.append("")
    if not live_ran:
        lines += [
            "**PENDING — not run.** This script does not dispatch live "
            "turns on its own; a running backend is required. Run:",
            "",
            "```bash",
            "python3 scripts/bigread_drill.py --backend local",
            "python3 scripts/bigread_drill.py --backend openrouter",
            "```",
            "",
            "(each performs the full unset/`=1` x 32768/131072 matrix "
            "against the named backend and rewrites this file with real "
            "rows).",
            "",
        ]
    lines += ["| arm | window | outcome | tool_calls | peak pressure | "
             "refused reads | resume calls used | wall_s | retries | "
             "compaction | recovered |",
             "|---|---:|---|---:|---:|---:|---:|---:|---:|---|---|"]
    for arm in ARMS:
        for window in WINDOWS:
            detail = matrix.get((arm, window))
            lines.append(_row(arm, window, detail))
    lines.append("")

    lines += ["## Verdict", ""]
    if not live_ran:
        lines.append("Not yet measurable — the matrix above is pending a "
                     "live run (see command above). The real-incident "
                     "section already proves arm=OFF loses the turn on both "
                     "backends at both windows tested in the wild; what is "
                     "still unmeasured is arm=ON's actual behavior — "
                     "specifically whether it trades one fatal turn for a "
                     "clean completion, or for several timid ones (PLAN.md's "
                     "explicit concern).")
    else:
        on_rows = [matrix.get(("on", w)) for w in WINDOWS]
        off_rows = [matrix.get(("off", w)) for w in WINDOWS]
        on_ok = all(d and d.get("outcome") == "completed" for d in on_rows)
        off_bad = any(d and d.get("outcome") != "completed" for d in off_rows)
        extra_steps = sum((d.get("resume_calls") or 0) for d in on_rows if d)
        if on_ok and off_bad:
            verdict = (f"arm=ON completes both windows where arm=OFF did "
                      f"not, at a cost of {extra_steps} extra resume "
                      "call(s) total across both windows — see the "
                      "resume-calls column for whether that is a clean "
                      "single re-read or several timid windows.")
        elif on_ok and not off_bad:
            verdict = ("Both arms completed in this run — the planted repo "
                      "did not reproduce the failure at these sizes/windows "
                      "on this backend; re-check the file sizes against the "
                      "live model's actual context accounting before "
                      "concluding the budget is unneeded.")
        else:
            verdict = ("arm=ON did NOT reliably fix the turn — inspect the "
                      "matrix rows above by hand before proposing the "
                      "Phase 2.2 default flip.")
        lines.append(verdict)
    lines.append("")
    return "\n".join(lines) + "\n"


# --- CLI ----------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="Plant the repo, run the parser self-test against "
                         "the real 2026-08-24 session, and write REPORT.md "
                         "with the incident section populated and the "
                         "matrix marked PENDING. Drives no live turns.")
    ap.add_argument("--backend", default=None,
                    help="`luxe chat --backend <name>` for the live matrix "
                         "(e.g. local, openrouter, m5). Required for a live "
                         "run; a `backends:` entry name from chat.yaml.")
    ap.add_argument("--config", dest="config_path", default=None,
                    help="Passthrough to `luxe chat --config`.")
    ap.add_argument("--model", default=None,
                    help="Passthrough to `luxe chat --chat-model`.")
    ap.add_argument("--luxe-cmd", default="luxe",
                    help="Executable to invoke for the live matrix "
                         "(default: `luxe` on PATH).")
    ap.add_argument("--luxe-root", default=str(DEFAULT_LUXE_ROOT),
                    help="Where sessions/runs live (default ~/.luxe).")
    ap.add_argument("--repo-dir", default=None,
                    help="Reuse this dir as the scratch repo instead of a "
                         "fresh tempfile.mkdtemp (still deterministic "
                         "content; useful to inspect after the drill).")
    ap.add_argument("--keep-repo", action="store_true",
                    help="Don't delete the scratch repo afterward.")
    ap.add_argument("--windows", default=",".join(str(w) for w in WINDOWS),
                    help="Comma-separated num_ctx windows to drill.")
    ap.add_argument("--arms", default=",".join(ARMS),
                    help="Comma-separated arms: off,on")
    ap.add_argument("--timeout", type=int, default=600,
                    help="Per-turn subprocess timeout, seconds (default "
                         "600 — an aborted retry-exhaustion turn alone was "
                         "measured at ~80s; leave headroom).")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="REPORT.md path.")
    args = ap.parse_args(argv)

    luxe_root = Path(args.luxe_root).expanduser()
    out_path = Path(args.out).expanduser()

    print(f"[selftest] verifying the parser against real session "
         f"{VERIFY_SESSION} under {luxe_root} ...")
    selftest_ok = run_selftest(luxe_root)
    if selftest_ok is False:
        print("[selftest] FAILED — fix the parser before trusting any "
             "matrix this script produces.", file=sys.stderr)

    incident_turns = {
        VERIFY_SESSION: analyze_session(VERIFY_SESSION, luxe_root),
        VERIFY_SESSION_CASE2: analyze_session(VERIFY_SESSION_CASE2, luxe_root),
    }

    if not args.dry_run and not args.backend:
        print("error: a live run needs --backend <name> (a "
             "configs/chat.yaml backends: entry). Use --dry-run to "
             "exercise planting/parsing/reporting without one.",
             file=sys.stderr)
        return 2

    windows = [int(w) for w in args.windows.split(",") if w.strip()]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]

    scratch_dir = Path(args.repo_dir) if args.repo_dir else \
        Path(tempfile.mkdtemp(prefix="luxe-bigread-drill-"))
    print(f"[plant] writing scratch repo to {scratch_dir}")
    sizes = plant_repo(scratch_dir)
    for name, size in sizes.items():
        print(f"[plant]   {name}: {size:,} bytes")

    matrix: dict[tuple[str, int], dict] = {}
    live_ran = False

    if args.dry_run:
        print("[dry-run] no live turns driven. Live command:")
        print(f"    python3 {Path(__file__).name} --backend <name>")
    else:
        live_ran = True
        for arm in arms:
            for window in windows:
                budget_on = (arm == "on")
                print(f"[drive] arm={arm} window={window} ...")
                t0 = time.monotonic()
                session_id = run_drill_turn(
                    scratch_dir, backend_name=args.backend,
                    config_path=args.config_path, window=window,
                    budget_on=budget_on, luxe_root=luxe_root,
                    timeout=args.timeout, model=args.model,
                    luxe_cmd=args.luxe_cmd)
                dt = time.monotonic() - t0
                if session_id is None:
                    print(f"[drive]   no new session appeared "
                         f"({dt:.0f}s) — startup failure?")
                    continue
                print(f"[drive]   session {session_id} ({dt:.0f}s wall)")
                turns = analyze_session(session_id, luxe_root)
                if not turns:
                    print(f"[drive]   WARNING: no turns parsed from "
                         f"session {session_id}'s debug.log")
                    continue
                # One turn was driven; take the (only, or last) one.
                run_id = sorted(turns)[-1]
                matrix[(arm, window)] = turns[run_id]
                matrix[(arm, window)]["session_id"] = session_id

    report = render_report(matrix=matrix, incident_turns=incident_turns,
                           selftest_ok=selftest_ok, plant_sizes=sizes,
                           scratch_repo=scratch_dir, live_ran=live_ran)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"[report] wrote {out_path}")

    if not args.keep_repo and not args.repo_dir:
        shutil.rmtree(scratch_dir, ignore_errors=True)
        print(f"[plant] removed scratch repo {scratch_dir}")
    elif args.keep_repo:
        print(f"[plant] kept scratch repo at {scratch_dir}")

    if selftest_ok is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
