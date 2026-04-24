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

from luxe import runner as _runner
from luxe.registry import LuxeConfig
from luxe.router import RouterDecision, route as _route
from luxe.session import Session
from luxe.tasks.model import Subtask, Task, _now, append_log_event, persist
from luxe.tools import fs as _fs


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
            })

        if aborted:
            task.status = "aborted"
        else:
            all_ok = all(s.status in ("done", "skipped") for s in task.subtasks)
            task.status = "done" if all_ok else "blocked"
        task.completed_at = _now()
        persist(task)
        self._emit(task, {"event": "finish", "status": task.status})
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
        decision = RouterDecision(
            agent=agent,
            task=augmented,
            reasoning=f"task orchestrator (subtask {sub.id})",
        )
        result = _runner.dispatch(decision, self.cfg, session=self.session)
        sub.agent = agent
        sub.result_text = result.final_text or ""
        sub.tool_calls = list(result.tool_calls)
        sub.tool_calls_total = result.tool_calls_total
        sub.steps_taken = result.steps_taken
        sub.prompt_tokens = result.prompt_tokens
        sub.completion_tokens = result.completion_tokens
        sub.near_cap_turns = result.near_cap_turns
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
                retry_decision, self.cfg, session=self.session
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
                        forced_decision, self.cfg, session=self.session
                    )
                    sub.result_text = forced_result.final_text or sub.result_text
                    # Not real agent tool calls, but the orchestrator
                    # did run greps so we count them for visibility.
                    sub.tool_calls_total += forced.count("```grep-output")
                    sub.prompt_tokens += forced_result.prompt_tokens
                    sub.completion_tokens += forced_result.completion_tokens
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

        # Ground-truthing: for review/refactor, verify every `file:line`
        # the agent cited actually exists in the repo and the line is in
        # range. Fake citations get annotated into sub.result_text so the
        # final report makes the unreliability obvious.
        if agent in ("review", "refactor") and sub.result_text:
            bad = _verify_citations(sub.result_text)
            if bad:
                self._emit(task, {
                    "event": "grounding_issues",
                    "subtask": sub.id,
                    "count": len(bad),
                })
                sub.result_text = _annotate_bad_citations(sub.result_text, bad)
                suffix_note = (
                    f"{len(bad)} unverified citation(s) — "
                    "see grounding warning in report"
                )
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


def _annotate_bad_citations(
    text: str, bad: list[tuple[str, int, str]]
) -> str:
    """Prepend a grounding-warning block to a subtask's output so the
    final report makes it visible which findings couldn't be verified.
    Stripping findings automatically is too error-prone with free-form
    markdown — annotation is the honest middle ground."""
    if not bad:
        return text
    lines = [
        "> ⚠️ **Grounding check failed for the following citations** — "
        "treat findings that reference these locations as unverified:"
    ]
    for path, line, reason in bad:
        lines.append(f"> - `{path}:{line}` — {reason}")
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
        return (
            "\n\n# Scope for THIS subtask (strict)\n"
            f"This subtask is scoped to ONE category: `{title}`. Only "
            "report findings in that category. Do NOT emit the final "
            "severity-grouped report (a later synthesis subtask will). "
            "Do NOT re-report findings that a prior subtask already "
            "listed above — the synthesis pass will merge them. Ground "
            "every finding in code you read with `read_file` or `grep` "
            "in this turn; if you didn't read it, don't cite it."
        )
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
