"""Validator agent — verifies worker citations and emits a structured envelope.

Output contract (rev 2): the validator's final_text MUST be a JSON object
matching ValidatorEnvelope. The orchestrator parses it; downstream stages
(synthesizer, citation linter) consume the structured form, not prose.

The structured envelope kills the failure mode where prose-only validators
silently strip every finding and look identical to a legitimate all-clear.
The `status` field disambiguates:
  - "cleared"   — workers had nothing to verify (legit all-clear)
  - "verified"  — at least one finding survived verification
  - "ambiguous" — >50% of findings were removed (synthesizer flags the run)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from luxe.agents.loop import AgentResult, OnToolEvent, run_agent
from luxe.backend import Backend
from luxe.config import RoleConfig
from luxe.tools import fs, git
from luxe.tools.base import ToolCache


@dataclass
class ValidatorFinding:
    path: str
    line: int
    snippet: str
    severity: str = "info"
    description: str = ""


@dataclass
class ValidatorRemoved:
    original: str
    reason: str = ""  # file_not_found | line_mismatch | content_mismatch | malformed


@dataclass
class ValidatorEnvelope:
    status: str = "cleared"  # cleared | verified | ambiguous
    verified: list[ValidatorFinding] = field(default_factory=list)
    removed: list[ValidatorRemoved] = field(default_factory=list)
    summary: str = ""

    @property
    def is_ambiguous(self) -> bool:
        return self.status == "ambiguous"

    @property
    def is_cleared(self) -> bool:
        return self.status == "cleared"


_SYSTEM_PROMPT = """\
You are a citation verifier. Your job is to verify each finding from code
analysis workers by checking its file:line citation against the actual code.

Process:
1. For each finding, use `read_file` to load the cited path; use `grep` if you
   need to search for the cited content elsewhere.
2. If the file does not exist or the cited code does not match, REMOVE the
   finding (do not flag it; remove it).
3. For each finding you keep, copy 1–3 lines of the actual code at the cited
   line into a `snippet` field. The downstream synthesizer and linter use this
   to verify the report against post-edit repo state.
4. You may NOT add new findings. You may only verify or remove.

Output: a single JSON object on its own line, no prose, no markdown fence.

Schema:
{
  "status": "verified" | "cleared" | "ambiguous",
  "verified": [
    {
      "path": "src/foo.py",
      "line": 42,
      "snippet": "...exact code at the cited line(s), 1-3 lines verbatim...",
      "severity": "critical" | "high" | "medium" | "low" | "info",
      "description": "..."
    }
  ],
  "removed": [
    { "original": "<the original finding text>", "reason": "file_not_found|line_mismatch|content_mismatch" }
  ],
  "summary": "<1-2 sentence overall assessment>"
}

Status rules:
- "cleared"   — input had zero findings to verify; emit empty `verified` and `removed`.
- "verified"  — at least one finding was kept; <50% of input findings removed.
- "ambiguous" — input had findings but more than half were removed (this is a
                signal the workers may have hallucinated; the synthesizer will
                flag the run for re-running, but the run still completes).

Output ONLY the JSON object. No explanation.
"""


def _extract_json(text: str) -> str | None:
    """Find the largest balanced JSON object in `text`. Tolerant of fences and prose."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def parse_envelope(text: str, input_finding_count: int) -> ValidatorEnvelope:
    """Parse the validator's final_text into a ValidatorEnvelope.

    Robust to common LLM output quirks (markdown fences, prose preamble). On
    parse failure: returns an `ambiguous` envelope wrapping the raw text in the
    `summary` field — the synthesizer will surface the warning to the user.

    `input_finding_count` is used to compute the cleared/verified/ambiguous
    thresholds when the model chooses inconsistent status values.
    """
    raw = _extract_json(text or "") or ""
    if not raw:
        return ValidatorEnvelope(
            status="ambiguous" if input_finding_count > 0 else "cleared",
            removed=[ValidatorRemoved(original=text or "", reason="malformed")],
            summary="Validator output could not be parsed as JSON; treat findings as unverified.",
        )

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return ValidatorEnvelope(
            status="ambiguous" if input_finding_count > 0 else "cleared",
            removed=[ValidatorRemoved(original=raw, reason=f"malformed: {e}")],
            summary="Validator output JSON parse failed; treat findings as unverified.",
        )

    verified: list[ValidatorFinding] = []
    for v in obj.get("verified", []) or []:
        if not isinstance(v, dict):
            continue
        try:
            verified.append(ValidatorFinding(
                path=str(v.get("path", "")).strip(),
                line=int(v.get("line", 0) or 0),
                snippet=str(v.get("snippet", "")).strip(),
                severity=str(v.get("severity", "info")).strip().lower(),
                description=str(v.get("description", "")).strip(),
            ))
        except (TypeError, ValueError):
            continue

    removed: list[ValidatorRemoved] = []
    for r in obj.get("removed", []) or []:
        if not isinstance(r, dict):
            continue
        removed.append(ValidatorRemoved(
            original=str(r.get("original", "")).strip(),
            reason=str(r.get("reason", "")).strip(),
        ))

    # Re-derive status from counts if model claimed something inconsistent.
    declared = str(obj.get("status", "")).strip().lower()
    n_verified = len(verified)
    n_removed = len(removed)
    n_total = n_verified + n_removed

    if declared in {"cleared", "verified", "ambiguous"}:
        status = declared
    elif n_total == 0:
        status = "cleared"
    elif n_verified == 0:
        status = "ambiguous"
    elif n_removed > n_verified:  # >50% removed
        status = "ambiguous"
    else:
        status = "verified"

    # Sanity-correct: if the model said "cleared" but we have findings, override.
    if status == "cleared" and n_total > 0:
        status = "ambiguous" if n_removed > n_verified else "verified"
    # If the model said "verified" but >50% were removed, treat as ambiguous.
    if status == "verified" and n_total > 0 and n_removed > n_verified:
        status = "ambiguous"

    return ValidatorEnvelope(
        status=status,
        verified=verified,
        removed=removed,
        summary=str(obj.get("summary", "")).strip(),
    )


def run_validator(
    backend: Backend,
    role_cfg: RoleConfig,
    *,
    worker_findings: str,
    cache: ToolCache | None = None,
    on_tool_event: OnToolEvent | None = None,
) -> tuple[AgentResult, ValidatorEnvelope]:
    """Verify worker findings and return both the agent result and the parsed envelope."""

    defs = [d for d in fs.read_only_defs() if d.name in {"read_file", "grep"}]
    defs.extend(d for d in git.tool_defs() if d.name == "git_diff")
    fns = {
        "read_file": fs.READ_ONLY_FNS["read_file"],
        "grep": fs.READ_ONLY_FNS["grep"],
        "git_diff": git.TOOL_FNS["git_diff"],
    }
    cacheable = {"read_file", "grep"}

    if not worker_findings.strip():
        # Cleared without invoking the model — zero findings means zero work.
        empty = AgentResult()
        empty.final_text = '{"status":"cleared","verified":[],"removed":[],"summary":"No worker findings to verify."}'
        return empty, ValidatorEnvelope(status="cleared", summary="No worker findings to verify.")

    task_prompt = (
        "Verify the following findings from code analysis workers. "
        "Check every file:line citation. Output ONLY the JSON envelope.\n\n"
        f"{worker_findings}"
    )

    result = run_agent(
        backend, role_cfg,
        system_prompt=_SYSTEM_PROMPT,
        task_prompt=task_prompt,
        tool_defs=defs,
        tool_fns=fns,
        cache=cache,
        cacheable=cacheable,
        on_tool_event=on_tool_event,
    )

    # Estimate input finding count (lines that look like a citation) for status fallback.
    input_count = len(re.findall(r"\b[\w/.-]+\.\w+:\d+", worker_findings))
    envelope = parse_envelope(result.final_text, input_count)
    return result, envelope
