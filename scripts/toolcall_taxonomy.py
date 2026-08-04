#!/usr/bin/env python3
"""Mine `~/.luxe/` for tool-call failure classes. Read-only, stdlib only.

Evidence before code (the C1 half of the 2026-08-04 operability cycle): before
hardening anything in `agents/loop.py` or `tools/base.py`, count what actually
goes wrong on this host. A class earns a fix only at **≥5 occurrences across
≥2 distinct sessions** inside the window.

Sources, joined on `run_id = f"{session_id}-{turn_idx}"`:

  ~/.luxe/runs/<run_id>/events.jsonl       per-step `tool_call` + `single_mode_done`
  ~/.luxe/sessions/<id>/transcript.jsonl   per-turn assistant/error records
  ~/.luxe/sessions/<id>/debug.log          plain-text session log

Usage:
    python scripts/toolcall_taxonomy.py [--days 45] [--out <REPORT.md>]
                                        [--luxe-root ~/.luxe] [--quiet]

Buckets it cannot measure with today's records are reported as UNMEASURABLE
with the reason, never guessed at — an invented denominator is worse than a
missing one.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# The tool surface, used to classify a dispatched name as executable or not.
# Hand-maintained lists go stale silently and produce a WRONG denominator (a
# first draft of this script counted the real `cve_lookup`/`git_show`/
# `deps_audit` as hallucinations), so prefer the live registry when luxe is
# importable and fall back to this snapshot only when it isn't.
_FALLBACK_TOOLS = {
    # agents/single.py `_build_full_tool_surface`, all task types (2026-08-04)
    "bash", "bm25_search", "cve_lookup", "deps_audit", "edit_file",
    "find_symbol", "git_diff", "git_log", "git_show", "glob", "grep",
    "lint", "lint_js", "lint_rust", "list_dir", "read_file", "security_scan",
    "typecheck", "typecheck_ts", "vet_go", "write_file",
    "respond",                       # LUXE_RESPOND_TERMINAL=1 only
    # chat-only extra-tool seam (chat/repl.py `prepare_turn`)
    "update_ledger", "net_probe", "planeproxy_diag",
    "web_fetch", "web_search", "web_answer",
}
MCP_PREFIX = "mcp__"

#: Run-id prefixes belonging to BENCH APPARATUS, not agent behaviour: the
#: chunk-conclude A/B replay harness dispatches synthetic names (`foo`) by
#: construction, and the smoke drills are scripted. Counting them would put
#: harness noise above the evidence bar. Overridable with --include-apparatus.
APPARATUS_PREFIXES = ("ccab-", "smoke-", "test-", "capdrill-")


def known_tools() -> tuple[set[str], str]:
    """(names, provenance). Live registry when importable, snapshot otherwise."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from luxe.agents.single import _build_full_tool_surface
        langs = frozenset({"python", "javascript", "typescript", "go", "rust"})
        names: set[str] = set()
        for tt in ("manage", "implement", "review", "document", "bugfix",
                   "summarize", None):
            names |= set(_build_full_tool_surface(langs, None, tt)[1])
        names |= {"respond", "update_ledger", "net_probe", "planeproxy_diag",
                  "web_fetch", "web_search", "web_answer"}
        return names, "live registry (_build_full_tool_surface)"
    except Exception:
        return set(_FALLBACK_TOOLS), "static snapshot (luxe not importable)"

#: A class needs this much evidence before it justifies code.
BAR_OCCURRENCES = 5
BAR_SESSIONS = 2

_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>")
_RETRY_RE = re.compile(
    r"(transient-\w+|5xx-transient-\S+|5xx-terminal-\S+|5xx-empty-\w+|"
    r"4xx-\d+|unknown-error-\w+|exhausted-attempts|unexpected-\d+)")


@dataclass
class Bucket:
    """One failure class: how often, in how many distinct sessions, examples."""
    name: str
    what: str                       # what it means / why it would matter
    measurable: bool = True
    why_unmeasurable: str = ""
    count: int = 0
    sessions: set = field(default_factory=set)
    examples: list = field(default_factory=list)
    by_key: collections.Counter = field(default_factory=collections.Counter)

    def hit(self, session: str, example: str, key: str = "") -> None:  # noqa: D102
        self.count += 1
        self.sessions.add(session)
        if key:
            self.by_key[key] += 1
        if len(self.examples) < 8:
            self.examples.append(example)

    @property
    def clears_bar(self) -> bool:
        return (self.measurable
                and self.count >= BAR_OCCURRENCES
                and len(self.sessions) >= BAR_SESSIONS)

    @property
    def verdict(self) -> str:
        if not self.measurable:
            return "UNMEASURABLE"
        if self.count == 0:
            return "no occurrences"
        if self.clears_bar:
            return "CLEARS BAR"
        return (f"below bar ({self.count} occ / {len(self.sessions)} sess; "
                f"need {BAR_OCCURRENCES}/{BAR_SESSIONS})")


def _session_of(run_id: str) -> str:
    """`<session_id>-<turn_idx>` → session id; non-chat run ids pass through."""
    if "-" in run_id and run_id.rsplit("-", 1)[1].isdigit():
        return run_id.rsplit("-", 1)[0]
    return run_id


def _iter_jsonl(path: Path):
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


def _walk_dirs(root: Path):
    """One level of children — never `rglob` a user root (luxe.sdd)."""
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.is_dir():
                    yield Path(entry.path)
    except OSError:
        return


def collect(luxe_root: Path, cutoff: float, *,
            include_apparatus: bool = False) -> tuple[dict, dict]:
    """Scan runs + sessions once. Returns (buckets, stats)."""
    tools, tools_from = known_tools()
    b = {
        "schema_reject": Bucket(
            "schema_reject",
            "validate_args refused the arguments; the model got 'Schema error: …' "
            "back and had to self-correct"),
        "unknown_tool_name": Bucket(
            "unknown_tool_name",
            "a dispatched name outside the known tool surface → 'Unknown tool: X'"),
        "duplicate_storm": Bucket(
            "duplicate_storm",
            "a run where ≥3 tool calls were exact-argument repeats (dedup fired)"),
        "textfallback_drop": Bucket(
            "textfallback_drop",
            "assistant prose containing a literal <tool_call> block with no tool "
            "call recorded that turn — the candidate was parsed and DROPPED, and "
            "the model got no feedback at all"),
        "empty_response": Bucket(
            "empty_response",
            "a completed (non-interrupted) turn whose assistant text was empty"),
        "aborted_run": Bucket(
            "aborted_run",
            "the loop aborted (step budget, watchdog, backend); abort_reason kept"),
        "turn_error": Bucket(
            "turn_error",
            "kind='error' transcript record — the turn raised and was reported"),
        "backend_retry": Bucket(
            "backend_retry",
            "backend.chat retry decisions (transient-*, 5xx-*, 4xx-*)"),
    }
    stats = collections.Counter()
    stats["_tools_known"] = len(tools)
    meta = {"tools_from": tools_from,
            "apparatus": "included" if include_apparatus
                         else ", ".join(APPARATUS_PREFIXES)}

    # --- runs/<run_id>/events.jsonl -----------------------------------------
    # As of 2026-08-04 the loop emits DIRECT events for three classes that
    # previously needed proxies: `tool_reject` (reason=schema|unknown_tool)
    # and `textfallback_drop`. Direct events are preferred; the legacy
    # proxies still run for older records but are suppressed wherever a
    # direct event already covered the same call, so mixed corpora never
    # double-count.
    runs_root = luxe_root / "runs"
    tool_calls_by_run: dict[str, int] = collections.Counter()
    run_seen_ts: dict[str, float] = {}
    direct_drop_runs: set[str] = set()
    for run_dir in _walk_dirs(runs_root):
        events = run_dir / "events.jsonl"
        if not events.is_file():
            continue
        run_id = run_dir.name
        if not include_apparatus and run_id.startswith(APPARATUS_PREFIXES):
            stats["runs_skipped_apparatus"] += 1
            continue
        session = _session_of(run_id)
        dups = 0
        in_window = False
        direct_schema = 0
        direct_unknown: set[tuple] = set()
        heur_unknown: list[tuple] = []
        for rec in _iter_jsonl(events):
            ts = float(rec.get("ts") or 0.0)
            if ts < cutoff:
                continue
            in_window = True
            run_seen_ts[run_id] = max(run_seen_ts.get(run_id, 0.0), ts)
            kind = rec.get("kind")
            if kind == "tool_call":
                stats["tool_calls"] += 1
                tool_calls_by_run[run_id] += 1
                name = (rec.get("name") or "").strip()
                if rec.get("duplicate"):
                    dups += 1
                if name and name not in tools \
                        and not name.startswith(MCP_PREFIX):
                    heur_unknown.append(
                        (rec.get("step"), name,
                         f"{run_id} step {rec.get('step')}: {name!r}"))
            elif kind == "tool_reject":
                reason = rec.get("reason") or ""
                name = (rec.get("name") or "").strip() or "(unnamed)"
                if reason == "schema":
                    direct_schema += 1
                    b["schema_reject"].hit(
                        session,
                        f"{run_id} step {rec.get('step')}: {name!r}: "
                        f"{(rec.get('message') or '')[:60]} (direct)",
                        key=name)
                elif reason == "unknown_tool":
                    direct_unknown.add((rec.get("step"), name))
                    b["unknown_tool_name"].hit(
                        session,
                        f"{run_id} step {rec.get('step')}: {name!r} (direct)",
                        key=name)
            elif kind == "textfallback_drop":
                direct_drop_runs.add(run_id)
                for nm in (rec.get("names") or ["(unnamed)"]):
                    b["textfallback_drop"].hit(
                        session,
                        f"{run_id} step {rec.get('step')}: {str(nm)!r} (direct)",
                        key=str(nm))
            elif kind == "single_mode_done":
                stats["runs_completed"] += 1
                # Direct per-call `tool_reject` events already counted their
                # share of this run's total; only the remainder (legacy runs
                # or pre-2026-08-04 records) lands via the old per-run proxy.
                rejects = max(
                    0, int(rec.get("schema_rejects") or 0) - direct_schema)
                for _ in range(rejects):
                    b["schema_reject"].hit(session, f"{run_id}: {rejects} reject(s)")
                if rec.get("aborted"):
                    reason = rec.get("abort_reason") or "(unreported)"
                    b["aborted_run"].hit(session, f"{run_id}: {reason}",
                                         key=str(reason))
        for step_, name_, example in heur_unknown:
            if (step_, name_) not in direct_unknown:
                b["unknown_tool_name"].hit(session, example, key=name_)
        if in_window:
            stats["runs_in_window"] += 1
            if dups >= 3:
                b["duplicate_storm"].hit(session, f"{run_id}: {dups} duplicates",
                                         key=str(dups))
            stats["duplicate_calls"] += dups

    # --- sessions/<id>/{transcript.jsonl,debug.log} -------------------------
    sessions_root = luxe_root / "sessions"
    for sess_dir in _walk_dirs(sessions_root):
        session = sess_dir.name
        turn = -1
        for rec in _iter_jsonl(sess_dir / "transcript.jsonl"):
            ts = float(rec.get("ts") or 0.0)
            kind = rec.get("kind")
            if kind == "user":
                turn += 1
            if ts < cutoff:
                continue
            if kind == "assistant":
                stats["assistant_turns"] += 1
                text = rec.get("text") or ""
                run_id = rec.get("run_id") or f"{session}-{max(turn, 0)}"
                if not rec.get("interrupted") and not text.strip():
                    b["empty_response"].hit(
                        session,
                        f"{run_id}: steps={rec.get('steps')} "
                        f"tools={rec.get('tool_calls')}")
                if _TOOL_CALL_TAG_RE.search(text) \
                        and not tool_calls_by_run.get(run_id) \
                        and run_id not in direct_drop_runs:
                    b["textfallback_drop"].hit(
                        session, f"{run_id}: <tool_call> in prose, 0 tool events")
            elif kind == "error":
                stats["error_records"] += 1
                text = (rec.get("text") or "").strip()
                b["turn_error"].hit(session, f"{session}: {text[:120]}",
                                    key=text.split(":")[0][:40] or "(bare)")

        log = sess_dir / "debug.log"
        if log.is_file():
            try:
                body = log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                body = ""
            stats["debug_log_lines"] += body.count("\n")
            for m in _RETRY_RE.finditer(body):
                b["backend_retry"].hit(session, f"{session}: {m.group(1)}",
                                       key=m.group(1))

    # Buckets today's records genuinely cannot answer.
    if stats["debug_log_lines"] == 0 or b["backend_retry"].count == 0:
        b["backend_retry"].measurable = False
        b["backend_retry"].why_unmeasurable = (
            "no retry decision lines found in any debug.log in the window. "
            "Sessions BEFORE 2026-08-04 never logged stream-path retries "
            "(the stream branch skipped the non-stream path's "
            "logger.warning); sessions after do, so a zero here on a "
            "post-2026-08-04 corpus is a genuinely quiet window, while a "
            "zero on an older corpus is a records gap.")
    stats["_meta"] = meta  # type: ignore[assignment]
    return b, stats


_UNMEASURABLE_NOTES = {
    "unknown_tool_name": (
        "Records from 2026-08-04 on carry a direct `tool_reject` event "
        "(reason=unknown_tool) with the result string — marked `(direct)` in "
        "the examples. Older records are approximated by the DISPATCHED NAME "
        "(a name outside KNOWN_TOOLS); calls covered by a direct event are "
        "excluded from the proxy so nothing double-counts."),
    "schema_reject": (
        "Records from 2026-08-04 on carry a direct per-call `tool_reject` "
        "event (reason=schema) with tool name + message — marked `(direct)`. "
        "Older records only have `single_mode_done.schema_rejects`, a per-run "
        "TOTAL with no per-tool breakdown; the proxy counts only the "
        "remainder not covered by direct events."),
    "textfallback_drop": (
        "Records from 2026-08-04 on carry a direct `textfallback_drop` event "
        "with the dropped name(s) — marked `(direct)`. Older records fall "
        "back to the proxy (a literal `<tool_call>` surviving into the "
        "assistant's prose with no tool event that run), suppressed for runs "
        "that already produced a direct event."),
}


def _display(key: str, limit: int = 70) -> str:
    """Markdown-safe, bounded rendering of a mined key.

    Load-bearing: one mined tool name was `list_dir` followed by HUNDREDS of
    newlines (a degenerate repetition loop), which rendered as a page of blank
    lines and silently broke the table. Keys are model output — never trust
    them to be one line.
    """
    shown = repr(key)[1:-1]
    return shown if len(shown) <= limit else shown[:limit] + "…"


def render(buckets: dict, stats: collections.Counter, *, days: int,
           luxe_root: Path) -> str:
    now = time.strftime("%Y-%m-%d %H:%M")
    meta = stats.get("_meta") or {"tools_from": "?", "apparatus": "?"}
    order = ["schema_reject", "unknown_tool_name", "textfallback_drop",
             "duplicate_storm", "empty_response", "aborted_run", "turn_error",
             "backend_retry"]
    lines = [
        "# Tool-call taxonomy — evidence before code",
        "",
        f"Generated {now} by `scripts/toolcall_taxonomy.py --days {days}` "
        f"over `{luxe_root}`.",
        "",
        "**Evidence bar:** a class justifies code only at "
        f"**≥{BAR_OCCURRENCES} occurrences across ≥{BAR_SESSIONS} distinct "
        "sessions** inside the window. Below the bar is a successful "
        "outcome — it means the fix would be speculative.",
        "",
        "## Corpus",
        "",
        "| | |",
        "|---|---|",
        f"| window | last {days} days |",
        f"| runs with events in window | {stats['runs_in_window']} |",
        f"| completed runs (`single_mode_done`) | {stats['runs_completed']} |",
        f"| tool calls | {stats['tool_calls']} |",
        f"| of those, exact-argument duplicates | {stats['duplicate_calls']} |",
        f"| assistant turns | {stats['assistant_turns']} |",
        f"| `error` transcript records | {stats['error_records']} |",
        f"| debug.log lines | {stats['debug_log_lines']} |",
        f"| apparatus runs skipped | {stats['runs_skipped_apparatus']} "
        f"(`{meta['apparatus']}`) |",
        f"| known tool names | {stats['_tools_known']} — {meta['tools_from']} |",
        "",
        "## Ranked: worth fixing?",
        "",
        "| class | occurrences | sessions | verdict |",
        "|---|---:|---:|---|",
    ]
    for key in order:
        bk = buckets[key]
        lines.append(f"| `{bk.name}` | {bk.count} | {len(bk.sessions)} | "
                     f"{bk.verdict} |")

    lines += ["", "## Detail", ""]
    for key in order:
        bk = buckets[key]
        lines += [f"### `{bk.name}` — {bk.verdict}", "", bk.what, ""]
        note = _UNMEASURABLE_NOTES.get(key)
        if note:
            lines += [f"> **Measurement:** {note}", ""]
        if not bk.measurable:
            lines += [f"> **UNMEASURABLE:** {bk.why_unmeasurable}", ""]
            continue
        lines.append(f"- occurrences: **{bk.count}** across "
                     f"**{len(bk.sessions)}** session(s)")
        if bk.by_key:
            top = ", ".join(f"`{_display(k)}` ×{v}"
                            for k, v in bk.by_key.most_common(6))
            lines.append(f"- top values: {top}")
        if bk.examples:
            lines.append("- examples:")
            lines += [f"  - `{_display(e, 160)}`" for e in bk.examples]
        lines.append("")

    cleared = [buckets[k].name for k in order if buckets[k].clears_bar]
    lines += [
        "## Conclusion",
        "",
        (f"Classes clearing the bar: **{', '.join(cleared)}**."
         if cleared else
         "**No class clears the evidence bar.** Per the cycle contract that "
         "is a successful outcome, not a gap: shipping a hardening fix for a "
         "failure mode this corpus does not exhibit would be speculative "
         "code on the benchmark path. The measurement gaps below are the "
         "actionable finding instead."),
        "",
        "### Record coverage (measurement gaps closed 2026-08-04)",
        "",
        "- `tool_reject` events (`{name, reason, message}`) now land in "
        "events.jsonl for schema rejects and unknown-tool dispatches; "
        "examples marked `(direct)` came from them. Records BEFORE "
        "2026-08-04 are still approximated by the legacy proxies.",
        "- `_parse_text_tool_calls` drops now emit a `textfallback_drop` "
        "event (and a debug.log warning) with the dropped names.",
        "- Backend retry decisions on the stream path (chat) now log the "
        "same `decision=` line as the non-stream path, so they reach "
        "debug.log wherever a session log handler is installed.",
        "",
    ]
    return "\n".join(lines) + "\n"


def render_context(buckets: dict, stats: collections.Counter, *,
                   days: int) -> str:
    """A wider window, appended as CONTEXT ONLY.

    A quiet six weeks is not evidence that a class does not exist, and a busy
    year is not evidence that it still does. Both facts belong in the report;
    only the contract window may clear the bar.
    """
    order = ["schema_reject", "unknown_tool_name", "textfallback_drop",
             "duplicate_storm", "empty_response", "aborted_run", "turn_error"]
    lines = [
        f"## Context: the last {days} days (NOT counted against the bar)",
        "",
        "The contract window above is the only one that may justify code. "
        "This wider scan exists so a quiet window is not misread as evidence "
        "of absence — and so a class that has been *fixed* can be told apart "
        "from one that never fired.",
        "",
        f"Corpus: {stats['runs_in_window']} runs, {stats['tool_calls']} tool "
        f"calls, {stats['assistant_turns']} assistant turns "
        f"({stats['runs_skipped_apparatus']} apparatus runs skipped).",
        "",
        "| class | occurrences | sessions | top values |",
        "|---|---:|---:|---|",
    ]
    for key in order:
        bk = buckets[key]
        top = ", ".join(f"`{_display(k, 40)}` ×{v}"
                        for k, v in bk.by_key.most_common(4)) or "—"
        lines.append(f"| `{bk.name}` | {bk.count} | {len(bk.sessions)} | {top} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--luxe-root", default=str(Path.home() / ".luxe"))
    ap.add_argument("--out", default="")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--context-days", type=int, default=0,
                    help="Also scan this much wider a window and append it as "
                         "CONTEXT ONLY (never counted against the bar) — the "
                         "45-day window can be quiet without the classes "
                         "being absent.")
    ap.add_argument("--include-apparatus", action="store_true",
                    help="Count bench-apparatus runs (ccab-/smoke-/test-/"
                         "capdrill-) too. Off by default: they dispatch "
                         "synthetic tool names by construction.")
    args = ap.parse_args(argv)

    root = Path(args.luxe_root).expanduser()
    if not root.is_dir():
        print(f"no luxe state at {root}", file=sys.stderr)
        return 1
    cutoff = time.time() - args.days * 86400
    buckets, stats = collect(root, cutoff,
                             include_apparatus=args.include_apparatus)
    report = render(buckets, stats, days=args.days, luxe_root=root)
    if args.context_days and args.context_days > args.days:
        wide, wstats = collect(root, time.time() - args.context_days * 86400,
                               include_apparatus=args.include_apparatus)
        report += render_context(wide, wstats, days=args.context_days)

    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        if not args.quiet:
            print(f"wrote {out}")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
