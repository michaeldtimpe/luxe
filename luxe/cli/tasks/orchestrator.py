"""Multi-subtask runner.

Drives a Task through its Subtasks, dispatching each to the appropriate
specialist via luxe.runner, persisting progress after every state change,
and enforcing the per-task wall budget. Phase 1 is synchronous — Phase 2
will wrap this in a subprocess for background runs.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

import httpx

from cli import runner as _runner
from cli.registry import LuxeConfig
from cli.router import RouterDecision, route as _route
from cli.session import Session
from cli.tasks.cache import ToolCache
from cli.tasks.model import Subtask, Task, _now, append_log_event, persist
from cli.tools import fs as _fs
from shared.trace_hints import parse_trace_paths


class Orchestrator:
    def __init__(
        self,
        cfg: LuxeConfig,
        session: Session | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.session = session
        # Optional live-event hook. The REPL wires this up in sync mode
        # to tail-print progress lines. Background subprocess runs leave
        # it None — their equivalent is log.jsonl + /tasks tail.
        self.on_event = on_event
        # Task-scoped tool-call cache. Freshly allocated at the start of
        # every `run()` so entries never leak across tasks (file mtimes
        # could change between invocations). See luxe/tasks/cache.py.
        self.tool_cache: ToolCache = ToolCache()

    def _emit(self, task: Task, event: dict[str, Any]) -> None:
        """Persist the event to log.jsonl AND fan out to the optional
        live-stream callback, if any."""
        append_log_event(task, event)
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001
                pass  # never let a broken UI sink abort a task

    def run(
        self,
        task: Task,
        should_abort: Callable[[], bool] = lambda: False,
    ) -> Task:
        """Drive `task` to completion. Idempotent: already-finished subtasks
        are skipped. `should_abort()` is polled at subtask boundaries so a
        SIGTERM to a background subprocess can stop the run cleanly."""
        if not task.subtasks:
            raise ValueError("task has no subtasks; plan before running")

        task.status = "running"
        # Reset task-scoped cache so resuming an older task doesn't
        # inherit a stale map from whatever Orchestrator last ran.
        self.tool_cache = ToolCache()
        persist(task)
        self._emit(task, {"event": "start", "n_subtasks": len(task.subtasks)})

        t0 = time.monotonic()
        aborted = False

        for sub in task.subtasks:
            if sub.status != "pending":
                continue

            if should_abort():
                aborted = True
                sub.status = "skipped"
                sub.error = "aborted before start"
                sub.completed_at = _now()
                persist(task)
                self._emit(task, {
                    "event": "skip", "subtask": sub.id, "reason": "aborted",
                })
                continue

            if time.monotonic() - t0 > task.max_wall_s:
                sub.status = "skipped"
                sub.error = "task wall budget exhausted"
                sub.completed_at = _now()
                persist(task)
                self._emit(task, {
                    "event": "skip", "subtask": sub.id,
                    "reason": "task wall budget",
                })
                continue

            sub.status = "running"
            sub.started_at = _now()
            persist(task)
            # Resolve the model the agent will use so the begin event
            # can surface it in the tail — makes it easy to spot when
            # review/refactor isn't actually landing on the coder model.
            agent_model = ""
            if sub.agent:
                try:
                    agent_model = self.cfg.get(sub.agent).model
                except (KeyError, Exception):  # noqa: BLE001
                    pass
            self._emit(task, {
                "event": "begin", "subtask": sub.id,
                "title": sub.title, "agent": sub.agent or "(route)",
                "model": agent_model,
            })

            # Snapshot cache counters to compute this subtask's delta.
            hits_before = self.tool_cache.hits
            misses_before = self.tool_cache.misses

            self._run_subtask(sub, task)

            if not sub.completed_at:
                sub.completed_at = _now()
            persist(task)
            self._emit(task, {
                "event": "end", "subtask": sub.id,
                "status": sub.status, "error": sub.error,
                "tool_calls": sub.tool_calls_total, "steps": sub.steps_taken,
                "wall_s": round(sub.wall_s, 1),
                "prompt_tokens": sub.prompt_tokens,
                "completion_tokens": sub.completion_tokens,
                "near_cap_turns": sub.near_cap_turns,
                "schema_rejects": sub.schema_rejects,
                "cache_hits": self.tool_cache.hits - hits_before,
                "cache_misses": self.tool_cache.misses - misses_before,
                "started_at": sub.started_at,
                "completed_at": sub.completed_at,
            })

        if aborted:
            task.status = "aborted"
        else:
            all_ok = all(s.status in ("done", "skipped") for s in task.subtasks)
            task.status = "done" if all_ok else "blocked"
        task.completed_at = _now()
        persist(task)
        self._emit(task, {
            "event": "finish",
            "status": task.status,
            # Totals for the whole task so the tail / bench harness can
            # see how effective cross-subtask memoization was without
            # walking every subtask record.
            "cache_hits": self.tool_cache.hits,
            "cache_misses": self.tool_cache.misses,
            "schema_rejects": sum(s.schema_rejects for s in task.subtasks),
        })
        return task

    # ── internals ──────────────────────────────────────────────────────

    def _run_subtask(self, sub: Subtask, task: Task) -> None:
        for attempt in range(2):  # initial + at most one retry
            try:
                self._dispatch_subtask(sub, task)
                return
            except (httpx.TransportError, ConnectionError) as e:
                if task.retry_on_transport_error and attempt == 0:
                    sub.attempt += 1
                    self._emit(task, {
                        "event": "retry_transport", "subtask": sub.id,
                        "error": f"{type(e).__name__}: {e}",
                    })
                    continue
                sub.status = "blocked"
                sub.error = f"{type(e).__name__}: {e}"
                return
            except KeyboardInterrupt:
                sub.status = "blocked"
                sub.error = "interrupted"
                task.status = "aborted"
                return
            except Exception as e:  # noqa: BLE001
                sub.status = "blocked"
                sub.error = f"{type(e).__name__}: {e}"
                return

    def _dispatch_subtask(self, sub: Subtask, task: Task) -> None:
        agent = sub.agent or self._pick_agent(sub.title)
        augmented = _augment_with_prior(task, sub)
        scope_note = _subtask_scope_note(sub, agent)
        if scope_note:
            augmented = augmented + scope_note
        # Pre-read files cited in pasted tracebacks so the agent
        # doesn't spend its first tool calls rediscovering them.
        # No-op when the task has no `path.py:LINE` mentions.
        trace_hints = _augment_with_trace_hints(task, sub, _fs.repo_root())
        if trace_hints:
            augmented = trace_hints + "\n\n" + augmented
        decision = RouterDecision(
            agent=agent,
            task=augmented,
            reasoning=f"task orchestrator (subtask {sub.id})",
        )
        dispatch_cfg = _cfg_with_task_overrides(self.cfg, agent, task, sub)
        _on_tool = lambda ev: self._emit(task, {**ev, "subtask": sub.id})
        result = _runner.dispatch(
            decision, dispatch_cfg,
            session=self.session, on_tool_event=_on_tool,
            tool_cache=self.tool_cache,
        )
        sub.agent = agent
        sub.result_text = result.final_text or ""
        sub.tool_calls = list(result.tool_calls)
        sub.tool_calls_total = result.tool_calls_total
        sub.steps_taken = result.steps_taken
        sub.prompt_tokens = result.prompt_tokens
        sub.completion_tokens = result.completion_tokens
        sub.near_cap_turns = result.near_cap_turns
        sub.schema_rejects = result.schema_rejects
        sub.wall_s = result.wall_s
        if result.aborted:
            sub.status = "blocked"
            sub.error = result.abort_reason or "agent aborted"
            return

        # Tool-use enforcement for review/refactor inspection subtasks.
        # A proper pass requires BOTH orientation (list_dir/glob) AND
        # real reading (read_file/grep). "1 call, list_dir only" is
        # barely better than 0 — the model peeked at filenames and
        # guessed. So the threshold is tool-type diversity, not just
        # a count.
        if (
            agent in ("review", "refactor")
            and _is_inspection_title(sub.title)
            and _inspection_too_shallow(
                result.tool_calls, sub.title, sub.result_text
            )
        ):
            shallow_reason = (
                "no tool calls at all"
                if result.tool_calls_total == 0
                else f"only used {', '.join({c.name for c in result.tool_calls})} — "
                     "no real reading"
            )
            self._emit(task, {
                "event": "tool_use_retry",
                "subtask": sub.id,
                "reason": f"inspection too shallow: {shallow_reason}",
            })
            nudge = (
                "\n\n# Retry — inspection was too shallow\n"
                f"Your previous attempt made shallow tool use ({shallow_reason}). "
                "A real inspection pass needs BOTH orientation (list_dir/glob "
                "to find files) AND reading (grep/read_file against specific "
                "files or patterns). Re-attempt now: first list or glob, then "
                "grep for relevant patterns or read specific files, then "
                "produce findings grounded in what you read.\n"
                "\n"
                "If the repo genuinely has no source files (just .gitignore "
                "or metadata), report that explicitly: 'Repo has no source "
                "files; no inspection surface.' Do NOT write literal "
                "placeholder phrases like 'I grepped X and read Y' — cite "
                "the actual files and patterns you used."
            )
            retry_decision = RouterDecision(
                agent=agent,
                task=augmented + nudge,
                reasoning=f"task orchestrator retry (subtask {sub.id}, shallow inspection)",
            )
            retry_result = _runner.dispatch(
                retry_decision, dispatch_cfg,
                session=self.session, on_tool_event=_on_tool,
                tool_cache=self.tool_cache,
            )
            # Accept the retry only if it did better on the depth axis.
            if not _inspection_too_shallow(
                retry_result.tool_calls, sub.title,
                retry_result.final_text or "",
            ):
                sub.result_text = retry_result.final_text or sub.result_text
                sub.tool_calls = list(retry_result.tool_calls)
                sub.tool_calls_total = retry_result.tool_calls_total
                sub.steps_taken += retry_result.steps_taken
                sub.prompt_tokens += retry_result.prompt_tokens
                sub.completion_tokens += retry_result.completion_tokens
                sub.schema_rejects += retry_result.schema_rejects
                sub.wall_s += retry_result.wall_s
            else:
                # The model refuses to call tools even when nudged twice.
                # Last resort: pre-execute a handful of inspection greps
                # ourselves and feed the results in, so the model has
                # real data to analyze. This converts "agent refuses to
                # use tools" into "agent summarizes data we handed it".
                self._emit(task, {
                    "event": "forced_inspection",
                    "subtask": sub.id,
                    "reason": "retry still didn't read files — pre-executing greps",
                })
                forced = _force_inspect(sub.title)
                if forced:
                    forced_decision = RouterDecision(
                        agent=agent,
                        task=augmented + "\n\n" + forced,
                        reasoning=f"task orchestrator forced-data (subtask {sub.id})",
                    )
                    forced_result = _runner.dispatch(
                        forced_decision, dispatch_cfg,
                        session=self.session, on_tool_event=_on_tool,
                        tool_cache=self.tool_cache,
                    )
                    sub.result_text = forced_result.final_text or sub.result_text
                    # Not real agent tool calls, but the orchestrator
                    # did run greps so we count them for visibility.
                    sub.tool_calls_total += forced.count("```grep-output")
                    sub.prompt_tokens += forced_result.prompt_tokens
                    sub.completion_tokens += forced_result.completion_tokens
                    sub.schema_rejects += forced_result.schema_rejects
                    sub.wall_s += forced_result.wall_s
                    sub.error = (
                        "agent wouldn't use tools — orchestrator "
                        "pre-ran inspection greps and had the agent "
                        "summarize (findings cite orchestrator data, "
                        "not agent-driven inspection)"
                    )
                else:
                    sub.error = (
                        "shallow inspection (retry also didn't read files) — "
                        "findings may not be grounded in code"
                    )

        # Ground-truthing: for review/refactor, verify (a) every
        # `file:line` the agent cited resolves to a real line, and
        # (b) every backtick-quoted code construct claimed in a
        # finding's Issue/Why text is an actual substring of the
        # cited file. (a) catches invented file paths and
        # out-of-range line numbers; (b) catches the more common
        # pattern where the model claims "server.py contains a call
        # to `os.system`" when it doesn't. Both get collected into a
        # single grounding warning prepended to sub.result_text so
        # the final report flags suspect findings visibly.
        if agent in ("review", "refactor") and sub.result_text:
            bad_cites = _verify_citations(sub.result_text)
            bad_patterns = _unverified_patterns(
                _extract_findings(sub.result_text)
            )
            total = len(bad_cites) + len(bad_patterns)
            if total:
                self._emit(task, {
                    "event": "grounding_issues",
                    "subtask": sub.id,
                    "count": total,
                    "bad_citations": len(bad_cites),
                    "bad_patterns": len(bad_patterns),
                })
                sub.result_text = _annotate_grounding_issues(
                    sub.result_text, bad_cites, bad_patterns
                )
                suffix_note = f"{total} unverified claim(s) — see grounding warning in report"
                sub.error = (
                    f"{sub.error}; {suffix_note}" if sub.error else suffix_note
                )

        sub.status = "done"

    def _pick_agent(self, title: str) -> str:
        """Use the router to pick an agent for an unassigned subtask. No
        clarifying questions allowed — this runs inside a task, not an
        interactive turn."""
        decision = _route(
            title, self.cfg,
            ask_fn=lambda _q: "",
            session=None,
        )
        return decision.agent


def _cfg_with_task_overrides(
    cfg: LuxeConfig, agent_name: str, task: Task, sub: Subtask | None = None
) -> LuxeConfig:
    """Apply per-subtask + per-task overrides to the dispatched agent's
    config. Only applies to review/refactor (the agents with a
    pre-flight survey behind their budgets). Returns the original cfg
    unchanged when no override applies, so this is free on the common
    path."""
    if agent_name not in ("review", "refactor"):
        return cfg
    updates: dict[str, Any] = {}
    # max_tokens_per_turn: subtask-level only — synthesis pass typically
    # wants a generous per-turn cap so the full report fits in one decode.
    if sub is not None and sub.max_tokens_per_turn_override is not None:
        updates["max_tokens_per_turn"] = sub.max_tokens_per_turn_override
    # analyzer_languages: task-level, set by /review's repo survey so the
    # review/refactor agents only see analyzers that match the repo's
    # languages.
    if task.analyzer_languages is not None:
        updates["analyzer_languages"] = frozenset(task.analyzer_languages)
    if not updates:
        return cfg
    new_agents = [
        a.model_copy(update=updates) if a.name == agent_name else a
        for a in cfg.agents
    ]
    return cfg.model_copy(update={"agents": new_agents})


_INSPECTION_VERBS = re.compile(
    r"\b(search|look|find|check|scan|inspect|identify|analyze|audit|"
    r"review\s+for|list\s+(directory|files)|"
    r"read\s+[\w\s,]*?\b(readme|architecture|arch|docs?|contributing|"
    r"security|changelog|license))\b",
    re.IGNORECASE,
)
_SYNTHESIS_VERBS = re.compile(
    r"\b(summari[zs]e|synthesi[zs]e|"
    r"(generate|write|produce|compile|assemble|emit)\s+[\w\s-]*?"
    r"\b(report|summary|writeup|write[\s-]?up))\b",
    re.IGNORECASE,
)


def _is_inspection_title(title: str) -> bool:
    """Heuristic: true if the subtask title sounds like code inspection
    (tool use expected), false if it sounds like synthesis (tool-free
    report generation from earlier findings)."""
    t = (title or "").strip()
    if not t:
        return False
    if _SYNTHESIS_VERBS.search(t):
        return False
    return bool(_INSPECTION_VERBS.search(t))


# Real inspection = orientation (list_dir / glob) + reading
# (grep / read_file). One-call-only or list_dir-only runs are flagged
# as shallow and retried with a stronger nudge.
_ORIENTATION_TOOLS = frozenset({"list_dir", "glob"})
_READING_TOOLS = frozenset({"read_file", "grep"})


_PURE_ORIENTATION_TITLE = re.compile(
    r"^\s*(list|show)\s+[\w\s-]*?"
    r"\b(directory|directories|dir|files|contents|tree|layout|structure)\b",
    re.IGNORECASE,
)


# Canonical starter greps for each inspection category. When the agent
# refuses to call tools twice in a row, the orchestrator runs these
# itself and hands the model the raw results to summarize — converting
# "I didn't look" into "here's what I found; now you analyze."
#
# Each recipe entry is (label, include_pattern, exclude_pattern). The
# exclude_pattern is applied line-by-line to the grep output to drop
# benign false positives (e.g. `secrets.token_hex` from Python's
# `secrets` module — a password-generator API, not a vuln).
_FORCED_INSPECTION_RECIPES: list[tuple[re.Pattern[str], list[tuple[str, str, str | None]]]] = [
    (
        re.compile(r"security", re.IGNORECASE),
        [
            ("dangerous-exec",
             r"eval\(|exec\(|os\.system|subprocess\.|shell_exec|popen",
             None),
            # The `secrets` stdlib module is the *safe* RNG API — exclude
            # its method calls so we stop flagging `secrets.token_hex`
            # and friends as potential leaks. Same for `os.urandom`.
            ("secrets",
             r"password|secret|api[-_]?key|token\s*=",
             r"\bsecrets\.(token_|choice|randbits|compare_digest|SystemRandom)"
             r"|\bos\.urandom\b"),
            ("deserialization",
             r"pickle\.loads|yaml\.load\(|marshal\.loads",
             None),
            ("sql-injection",
             r"(SELECT|INSERT|UPDATE|DELETE).*(\+|%s|f\")",
             None),
        ],
    ),
    (
        re.compile(r"correctness|bugs?", re.IGNORECASE),
        [
            ("bare-except",       r"except\s*:\s*$|except\s+Exception\s*:", None),
            ("todo-markers",      r"TODO|FIXME|XXX|HACK", None),
        ],
    ),
    (
        re.compile(r"robust", re.IGNORECASE),
        [
            ("network-no-timeout", r"requests\.|httpx\.|urllib\.request", None),
            ("unbounded-loops",    r"while\s+True", None),
        ],
    ),
    (
        re.compile(r"maintain|duplicat|dead\s+code", re.IGNORECASE),
        [
            ("function-density",   r"^def\s+|^class\s+", None),
            ("suppressions",       r"# noqa|# type:\s*ignore", None),
        ],
    ),
    (
        re.compile(r"performance|optimi[sz]", re.IGNORECASE),
        [
            ("nested-loops",       r"for\s+.*in\s+.*:", None),
            ("list-building",      r"\.append\(.*\).*for", None),
            ("uncached-regex",     r"re\.(compile|search|match)", None),
        ],
    ),
]


def _force_inspect(title: str) -> str:
    """When the agent refuses to call tools, run a small panel of greps
    ourselves and return the raw output as a ready-to-summarize prompt
    fragment. Returns empty string if no recipe matches the title."""
    recipes: list[tuple[str, str, str | None]] = []
    for pattern, grep_list in _FORCED_INSPECTION_RECIPES:
        if pattern.search(title):
            recipes = grep_list
            break
    if not recipes:
        return ""

    parts = [
        "# Orchestrator-run inspection (you refused to call tools twice; "
        "here is the data you need to summarize)",
        "",
    ]
    any_hits = False
    for label, pat, exclude in recipes:
        try:
            result, err = _fs.grep({"pattern": pat})
        except Exception as e:  # noqa: BLE001
            result, err = None, f"{type(e).__name__}: {e}"
        body = (result or "").strip() if err is None else f"ERROR: {err}"
        if body and body != "(no matches)" and exclude and err is None:
            keep = re.compile(exclude)
            body = "\n".join(
                line for line in body.splitlines()
                if not keep.search(line)
            ).strip()
        parts.append(f"```grep-output name={label} pattern={pat!r}")
        parts.append(body or "(no matches)")
        parts.append("```")
        parts.append("")
        if body and body != "(no matches)":
            any_hits = True

    parts.append(
        "# Your task\n"
        "Summarize the findings above. Rules — these are strict, not "
        "suggestions:\n"
        "- Cite the specific `file:line` exactly as it appears in the "
        "grep output above. Do NOT invent filenames or line numbers.\n"
        "- For any category whose grep output is `(no matches)`, output "
        "EXACTLY this single line for that category and nothing more:\n"
        "    **No findings in this category.**\n"
        "  No generic advice, no 'however it's still important to…', "
        "no speculation about what might exist. Empty means empty.\n"
        "- If every category is `(no matches)`, your whole answer is "
        "one line: **No findings in any inspected category.**\n"
        "- When grep output IS non-empty, every finding you emit must "
        "quote a real line from the output above. Do not paraphrase into "
        "hypothetical code (`query = f\"SELECT ...\"`) — that's "
        "hallucination."
    )
    return "\n".join(parts)


def _inspection_too_shallow(
    tool_calls, title: str = "", result_text: str = ""
) -> bool:
    """True when an inspection subtask didn't do enough to be credible.

    Pure-orientation subtasks ("List directory contents of the repo")
    are considered done as long as they called any tool — their whole
    purpose IS orientation. For inspection-proper subtasks ("Search
    for security issues", "Identify maintainability issues"), we need
    reading-tool use (grep/read_file), not just walking the tree.

    We also apply a citation/read-ratio check: if the final text cites
    many `file:line` locations but the agent ran only one reading call,
    the findings are almost certainly hallucinated from training
    knowledge rather than grounded in the repo. Flag as shallow so the
    retry-then-forced-inspection fallback kicks in.
    """
    if not tool_calls:
        return True
    # Orientation-only subtasks: any call counts as non-shallow.
    if _PURE_ORIENTATION_TITLE.search(title or ""):
        return False
    names = {c.name for c in tool_calls}
    did_read = bool(names & _READING_TOOLS)
    if not did_read:
        return True
    n_reads = sum(1 for c in tool_calls if c.name in _READING_TOOLS)
    n_cit = len(_extract_citations(result_text))
    # 1 read call citing 4+ file:line pairs is a hallmark of a model
    # that skimmed filenames and then invented findings.
    if n_cit >= 4 and n_reads < 2:
        return True
    return False


_CITATION_RE = re.compile(
    r"(?<![\w/])([\w][\w./-]*?\.(?:py|pyi|js|ts|tsx|jsx|go|rs|rb|java|"
    r"kt|kts|c|h|cc|cpp|hpp|cs|swift|m|mm|sh|bash|zsh|yaml|yml|toml|"
    r"json|md|txt|html|css|sql|tf|hcl|dockerfile|mk|cfg|ini|conf|service)):"
    r"(\d+)\b",
    re.IGNORECASE,
)


def _extract_citations(text: str) -> list[tuple[str, int]]:
    """Pull `path/to/file.ext:NNN` citations out of a subtask's final
    text. Extension list is explicit to avoid false matches on things
    like `http://host:8080` or `time 00:42`."""
    if not text:
        return []
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for m in _CITATION_RE.finditer(text):
        path, line = m.group(1), int(m.group(2))
        if (path, line) in seen:
            continue
        seen.add((path, line))
        out.append((path, line))
    return out


def _verify_citations(
    text: str,
) -> list[tuple[str, int, str]]:
    """For each unique `file:line` citation in `text`, confirm the file
    exists in the repo root and the line number is within range. Returns
    a list of `(path, line, reason)` for citations that did NOT verify.
    An empty list means every citation is grounded."""
    bad: list[tuple[str, int, str]] = []
    for path, line in _extract_citations(text):
        try:
            content, err = _fs.read_file({"path": path})
        except Exception as e:  # noqa: BLE001
            bad.append((path, line, f"{type(e).__name__}: {e}"))
            continue
        if err is not None:
            bad.append((path, line, err))
            continue
        n_lines = (content or "").count("\n") + (0 if not content else 1)
        if line < 1 or line > n_lines:
            bad.append(
                (path, line, f"line out of range (file has {n_lines} lines)")
            )
    return bad


_FILE_ANCHOR_RE = re.compile(
    # Optional list prefix (numbered or bulleted) before the anchor
    # itself. Allows both "1. **File: server.py**" and plain
    # "**File:** server.py".
    r"^\s*(?:(?:[-*]|\d+\.)\s+)*"
    r"\*\*\s*File:?\s*\*?\*?\s*`?([\w./-]+)`?\s*\*?\*?",
    re.IGNORECASE,
)

_FIELD_LINE_RE = re.compile(
    r"^\s*[-*]?\s*\*\*\s*(Issue|Why|Severity|Suggested\s+fix|Impact|"
    r"Proposed|Tradeoff|Current)\s*\*\*\s*:?\s*(.*)$",
    re.IGNORECASE,
)

# Only Issue/Why are assertions about what exists in the cited file.
# Suggested-fix / Proposed are recommendations — a model suggesting
# "use `subprocess.run`" isn't claiming `subprocess.run` is present.
_CLAIM_FIELDS = frozenset({"issue", "why"})

_CODE_TOKEN_RE = re.compile(r"`([^`\n]{2,120})`")


def _extract_findings(text: str) -> list[tuple[str, str]]:
    """Parse a review/refactor subtask's output and return
    `[(file_path, claim_text), ...]` — one tuple per **File:**-
    anchored finding block. `claim_text` is only the Issue/Why
    content; Suggested-fix text is excluded so that recommendations
    aren't treated as assertions."""
    if not text:
        return []
    out: list[tuple[str, str]] = []
    current_file: str | None = None
    current_claims: list[str] = []
    current_field: str | None = None

    def _flush() -> None:
        nonlocal current_file, current_claims, current_field
        if current_file and current_claims:
            out.append((current_file, " ".join(current_claims).strip()))
        current_file = None
        current_claims = []
        current_field = None

    for line in text.splitlines():
        fa = _FILE_ANCHOR_RE.match(line)
        if fa:
            _flush()
            current_file = fa.group(1).strip().rstrip(".,;:")
            continue
        fm = _FIELD_LINE_RE.match(line)
        if fm:
            field = fm.group(1).lower().replace(" ", "")
            current_field = field
            value = fm.group(2).strip()
            if field in _CLAIM_FIELDS and value:
                current_claims.append(value)
            continue
        # Continuation of a claim field (wrapped Why paragraph etc.)
        if current_field in _CLAIM_FIELDS and line.strip():
            current_claims.append(line.strip())
    _flush()
    return out


def _normalize_construct(tok: str) -> str:
    """Strip parenthesized call args so `os.system('ls')` and the
    bare `os.system` both check against the same base identifier. We
    keep `()` as a marker so `foo()` and `foo.bar` stay distinguishable."""
    return re.sub(r"\(.*?\)", "()", tok).strip()


def _unverified_patterns(
    findings: list[tuple[str, str]]
) -> list[tuple[str, str, str]]:
    """For each finding, extract backtick-quoted code-like tokens from
    its claim text (Issue/Why) and confirm each is an actual substring
    of the cited file. Returns `[(path, token, reason)]` for tokens
    that don't verify, plus one entry per finding whose cited file is
    missing from the repo."""
    bad: list[tuple[str, str, str]] = []
    cache: dict[str, str | None] = {}
    seen: set[tuple[str, str]] = set()
    for path, claim_text in findings:
        if path not in cache:
            try:
                content, err = _fs.read_file({"path": path})
                cache[path] = content if err is None else None
            except Exception:  # noqa: BLE001
                cache[path] = None
        content = cache[path]
        if content is None:
            key = (path, "")
            if key not in seen:
                seen.add(key)
                bad.append((path, "(cited file)", "file not found in repo"))
            continue
        for tok in _CODE_TOKEN_RE.findall(claim_text):
            t = tok.strip()
            if not t:
                continue
            # Skip the file path itself — _verify_citations already
            # handles file/line existence.
            if t == path or t.rstrip(".,;:") == path:
                continue
            # Only verify tokens that look structural (dotted/called/
            # underscored or contain a space like "except Exception").
            # Bare words like `file`, `main`, `query` would produce
            # noise — skip them.
            if not re.search(r"[._():\s]", t):
                continue
            normalized = _normalize_construct(t)
            if not normalized or len(normalized) < 3:
                continue
            if (path, normalized) in seen:
                continue
            seen.add((path, normalized))
            # Accept either the normalized form or the raw token as
            # substring evidence. Catches both `foo` and `foo(x)` when
            # the file has `foo(x)`.
            if normalized in content or t in content:
                continue
            # Also accept a stripped form (e.g. trailing colon removed)
            # so that "`except Exception:`" matches "except Exception:"
            # whether or not the trailing colon is in the source.
            if normalized.rstrip(":") in content:
                continue
            bad.append((path, t, "not present in cited file"))
    return bad


def _annotate_grounding_issues(
    text: str,
    bad_cites: list[tuple[str, int, str]],
    bad_patterns: list[tuple[str, str, str]],
) -> str:
    """Prepend a grounding-warning block listing both kinds of
    verification failure. Stripping fabricated findings automatically
    is too brittle on free-form markdown — annotating is the honest
    middle ground and keeps the synthesis subtask (which sees prior
    results) aware of what to distrust."""
    if not bad_cites and not bad_patterns:
        return text
    lines = [
        "> ⚠️ **Grounding check failed** — findings referencing the "
        "following could not be verified against the repo; treat them "
        "as suspect:"
    ]
    for path, line, reason in bad_cites:
        lines.append(f"> - `{path}:{line}` — {reason}")
    for path, tok, reason in bad_patterns:
        lines.append(f"> - claim `{tok}` in `{path}` — {reason}")
    lines.append("")
    return "\n".join(lines) + "\n" + text


def _subtask_scope_note(sub: Subtask, agent: str) -> str:
    """Per-subtask scope reminder. The planner decomposes a single user
    goal into N ordered subtasks, but the agent still sees the full
    goal text — which nudges models into answering the whole thing on
    subtask 1. The scope note tells the model what NOT to do in this
    particular subtask so that tool-use effort lands on the right pass.
    Only applied to review/refactor agents where this miscategorization
    has been observed in practice."""
    if agent not in ("review", "refactor"):
        return ""
    title = (sub.title or "").strip()
    if not title:
        return ""
    if _PURE_ORIENTATION_TITLE.search(title):
        return (
            "\n\n# Scope for THIS subtask (strict)\n"
            "This is an ORIENTATION subtask only. Call `list_dir` (or "
            "`glob`) once and report what you see: filenames and "
            "top-level directories. Do NOT produce any security, "
            "correctness, robustness, or maintainability findings — "
            "later subtasks will do that with the right tools. One "
            "paragraph listing what's present is the whole answer."
        )
    if _SYNTHESIS_VERBS.search(title):
        return (
            "\n\n# Scope for THIS subtask (strict)\n"
            "This is a SYNTHESIS subtask. Work only from the 'Prior "
            "findings' block above — do not run new tool calls and do "
            "not introduce findings that weren't in a prior subtask. "
            "Your job is to reorganize existing findings into a "
            "severity-grouped report."
        )
    if _is_inspection_title(title):
        tool_hint = _analyzer_hint_for_title(title)
        return (
            "\n\n# Scope for THIS subtask (strict)\n"
            f"This subtask is scoped to ONE category: `{title}`. Only "
            "report findings in that category. Do NOT emit the final "
            "severity-grouped report (a later synthesis subtask will). "
            "Do NOT re-report findings that a prior subtask already "
            "listed above — the synthesis pass will merge them. Ground "
            "every finding in code you read with `read_file` or `grep` "
            "in this turn; if you didn't read it, don't cite it."
            f"{tool_hint}"
        )
    return ""


# Category → analyzer tool hint. Emitted inside the scope note so the
# suggestion is visible at the right point — the generic "prefer real
# analyzers" line in the system prompt is easy to miss once the prior-
# findings block and scope note push it out of the model's near-term
# attention. Per-category hints surface the exact tool that matches
# the subtask's title.
_ANALYZER_HINTS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"security", re.IGNORECASE),
        "Start this subtask with FOUR tools, each covering a different "
        "dimension: `security_scan` (bandit — in-source security "
        "patterns), `deps_audit` (pip-audit — known-CVE deps), "
        "`security_taint` (semgrep — source→sanitizer→sink reasoning), "
        "and `secrets_scan` (gitleaks — hardcoded credentials, redacted). "
        "ALWAYS call `security_taint` before assigning High/Critical "
        "severity to any eval/exec/subprocess/pickle/SQL finding — "
        "semgrep's taint rules correctly ignore sandboxed or "
        "non-reachable sinks. Grep is the fallback for patterns outside "
        "these analyzers' coverage.",
    ),
    (
        re.compile(r"correctness|bugs?|error", re.IGNORECASE),
        "Start this subtask with `typecheck` (mypy). Most correctness "
        "bugs (wrong returns, unreachable branches, missing None "
        "checks) are type errors a grep can't find. Then `lint` for "
        "bare-except / unused-import / mutable-default patterns.",
    ),
    (
        re.compile(r"robust", re.IGNORECASE),
        "Start this subtask with `lint --select B` (ruff's bugbear "
        "rules catch loop/try/timeout robustness issues). `grep` is the "
        "fallback for patterns outside bugbear's coverage.",
    ),
    (
        re.compile(r"maintain|refactor|duplicat|dead\s+code", re.IGNORECASE),
        "Start this subtask with `lint` (ruff catches C901 complexity, "
        "F401/F841 dead code, B006 mutable defaults — pre-classified "
        "maintainability signals). Then grep for architectural patterns "
        "the linter doesn't address (duplicate logic, god-modules).",
    ),
    (
        re.compile(r"performance|optimi[sz]", re.IGNORECASE),
        "Start this subtask with `lint --select PERF,SIM` (ruff's "
        "perflint + simplify rules). Then `grep` for hot-path patterns "
        "the analyzer doesn't model (nested loops, repeated parsing).",
    ),
]


def _analyzer_hint_for_title(title: str) -> str:
    """Return a leading-newline-prefixed analyzer nudge string if the
    subtask title matches one of the known categories, else ''. Used
    by `_subtask_scope_note` to suggest the right analyzer tool for
    each category before the model reaches for grep."""
    for pattern, hint in _ANALYZER_HINTS:
        if pattern.search(title):
            return f"\n\n## Preferred tool for this category\n{hint}"
    return ""


def _summarize_result(text: str, max_chars: int = 800) -> str:
    """Trim a subtask's final_text for use as prior context. Prefer
    cutting on a sentence boundary so we don't hand the next subtask a
    half-finished thought."""
    t = (text or "").strip()
    if not t or len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    dot = cut.rfind(". ")
    if dot > int(max_chars * 0.5):
        return cut[: dot + 1] + " …"
    return cut + "…"


def _augment_with_prior(task: Task, sub: Subtask) -> str:
    """Prepend a terse summary of completed earlier subtasks to `sub`'s
    title so the dispatched agent can build on prior work. Serial
    execution guarantees a stable order; we never include blocked /
    skipped / pending peers."""
    prior = [
        s for s in task.subtasks
        if s.index < sub.index and s.status == "done" and s.result_text
    ]
    if not prior:
        return sub.title
    parts = ["# Prior findings in this task (use them; don't re-do them)"]
    for s in prior:
        parts.append(f"## Subtask {s.index}. {s.title}")
        parts.append(_summarize_result(s.result_text))
    parts.append("")
    parts.append("# Your task")
    parts.append(sub.title)
    return "\n\n".join(parts)


def _augment_with_trace_hints(
    task: Task,
    sub: Subtask,
    repo_root: Any,  # Path; Any to avoid importing at module top just for typing
    *,
    max_files: int = 3,
    max_lines: int = 200,
) -> str:
    """Pre-read files cited in pasted traceback output so the agent
    can start from real code instead of guessing paths with grep.

    Scans the subtask title plus prior completed subtasks' result_text
    for `path.py:LINE` references; resolves each against the repo
    root; skips paths that escape the repo or don't exist. Returns a
    Markdown block to prepend to the agent's prompt, or "" when
    nothing relevant was found (the common case for non-code tasks).

    This is the positive application of the compression-benchmark
    finding: selectivity-based pre-retrieval (oracle-style). We
    deliberately do NOT summarise or outline the files — that finding
    was negative on local coder models."""
    corpus_parts: list[str] = [sub.title or ""]
    for s in task.subtasks:
        if s.index < sub.index and s.status == "done" and s.result_text:
            corpus_parts.append(s.result_text)
    corpus = "\n".join(corpus_parts)

    seed_paths = parse_trace_paths(corpus, repo_root)
    if not seed_paths:
        return ""

    # Expand via import graph: for each seed, include its first-hop
    # neighbors (files it imports + files that import it) up to the
    # remaining budget. Whole-file reads only — summarisation regressed
    # in the compression benchmark. Graph errors are non-fatal.
    paths: list[Any] = []
    seen: set[Any] = set()
    for seed in seed_paths:
        if seed not in seen:
            paths.append(seed)
            seen.add(seed)
        if len(paths) >= max_files:
            break
    if len(paths) < max_files:
        try:
            from cli.import_graph import build_graph as _build_graph
            from cli.import_graph import neighbors as _neighbors
            graph = _build_graph(repo_root)
            remaining = max_files - len(paths)
            for seed in seed_paths:
                for n in _neighbors(graph, seed, max_neighbors=remaining):
                    if n in seen:
                        continue
                    paths.append(n)
                    seen.add(n)
                    remaining -= 1
                    if remaining <= 0:
                        break
                if remaining <= 0:
                    break
        except Exception:  # noqa: BLE001
            pass
    paths = paths[:max_files]

    blocks: list[str] = [
        "# Files mentioned in the error you're debugging",
        "(Pre-read by the orchestrator so you don't need to grep for them. "
        "Verify line numbers against the paste above before editing.)",
    ]
    repo_real = repo_root.resolve()
    for path in paths:
        try:
            rel = path.relative_to(repo_real)
        except ValueError:
            continue
        try:
            body = path.read_text(errors="ignore")
        except OSError:
            continue
        lines = body.splitlines()
        if len(lines) <= max_lines:
            blocks.append(f"\n## {rel}\n```\n{body}\n```")
        else:
            head = "\n".join(lines[:max_lines])
            blocks.append(
                f"\n## {rel} (first {max_lines} of {len(lines)} lines)\n"
                f"```\n{head}\n```"
            )
    return "\n".join(blocks)
