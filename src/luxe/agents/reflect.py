"""reflect.py — same-model verify primitive (Track 1 of the reflection cycle).

A standalone "second-look" critic that judges whether a produced output leaves the
task unsatisfied. **Verify-only by default**: it returns a structured `Verdict` and
changes nothing (Phase 1 measures whether the champion can separate its own gaps
from correct work). Phase 2 (repair) consumes the verdict to re-enter a bounded
corrective step — gated separately at the call sites.

ONE call shape + ONE verdict schema are shared across two driver surfaces so the
analysis scripts and production code can never drift:
  - **multi_turn (behavioral):** does the response leave any part of the user's
    stated request unaddressed?
  - **swebench (artifact):** does this code change resolve the described issue?

Design constraints (peer-reviewed; see the approved plan):
  - **temp=0, deterministic, no persistence.** The primitive is pure; opt-in gating
    (`LUXE_REFLECT`) lives at the call sites. Default-off → byte-identical generation.
  - **`gap = true` requires a FUNCTIONAL BLOCKER** — fails a core requirement,
    introduces a regression, or leaves a stated request unaddressed. Stylistic /
    suboptimal / "could be better" never flips the gap (defends the false-gap metric
    against temp=0 pedantic deficiency-inflation).
  - **Critique-only, anti-confound:** the verifier may NOT propose a new full
    solution, MUST cite concrete evidence from the existing output, and uses
    negative-space framing — so Phase 1 measures *self-detection*, not *second-pass
    solving*. Each deficiency's `specificity` is recorded for characterization.
  - **Generic prompts** — no benchmark-semantic phrasing (e.g. never "a tool call
    was warranted"); the multi_turn verifier asks about the *request being carried
    out*, not about tool calls (anti-overfitting).
  - **Sandwich context layout** — the task is restated at the high-attention tail,
    adjacent to the output under evaluation, so both anchors survive a long history.

This is the single source of truth for the reflect/verifier prompt surface; it is
distinct from the task-completion `PROMPT_REGISTRY` in `prompts.py` (see agents.sdd).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from luxe.backend import Backend


# --- env gating (call sites consult this; the primitive itself is pure) -------

def reflect_enabled() -> bool:
    """True iff the reflect stage is opted in. Default-off → byte-identical."""
    return os.environ.get("LUXE_REFLECT") == "1"


# --- verdict schema (shared; imported by the analysis + measurement scripts) --

# Specificity of a flagged deficiency — a Phase 1 characterization signal, NOT a
# gate input. A verifier that only fires when it has implicitly reconstructed the
# full fix ("solution_bearing") may not generalize online; we record rather than
# block that (per reviewer 3).
SPECIFICITY_VALUES = ("vague", "concrete_local", "solution_bearing")


@dataclass(frozen=True)
class Deficiency:
    what: str            # the unmet requirement / unaddressed ask
    evidence: str        # quoted from the output/transcript/diff
    specificity: str     # one of SPECIFICITY_VALUES (else "unknown")


@dataclass(frozen=True)
class Verdict:
    gap: bool                                   # True IFF >=1 substantiated functional blocker
    deficiencies: tuple[Deficiency, ...] = ()
    raw: str = ""                               # raw model text (debugging)
    error: str = ""                             # non-empty if the verdict was unparseable / call failed

    @property
    def ok(self) -> bool:
        """A usable verdict was produced (parsed, no call error)."""
        return not self.error


# --- prompt surface (single source; generic, critique-only, functional-blocker) --

_VERIFIER_SYSTEM = (
    "You are a strict, skeptical reviewer. Judge whether a piece of work fully does "
    "what was asked. Do NOT rewrite or re-solve it and do NOT propose a corrected "
    "version — only judge it and cite evidence. Report ONLY functional problems: "
    "something asked for but not done, a stated requirement left unmet, or a change "
    "that breaks correct behaviour. Never report style, naming, comments, idiom, or "
    "'could be better' — those are not problems here.\n\n"
    "Respond with ONLY this JSON object and nothing else:\n"
    '{"deficiencies": [{"what": "<the specific unmet thing>", '
    '"evidence": "<verbatim quote from the work, at most 15 words>", '
    '"specificity": "<vague|concrete_local|solution_bearing>"}]}\n\n'
    "One entry per functional problem. If the work fully does everything asked, "
    'return {"deficiencies": []}. The presence of any deficiency means there is a '
    'gap. For "specificity": "vague" = a general doubt you cannot pin to a spot; '
    '"concrete_local" = a specific gap you can point to; "solution_bearing" = naming '
    "the gap required you to work out the full correct answer yourself."
)

# multi_turn: behavioral, WHOLE-CONVERSATION framing. Deliberately phrased about the
# REQUESTS BEING CARRIED OUT, never about "tool calls" (anti-overfitting — agents.sdd /
# ship gate). Whole-conversation (not per-turn) is robust to the miss_func reveal
# mechanic, where a request from an earlier turn is meant to be fulfilled on a later
# (often message-less) turn; it also correctly ABSTAINS when the user already got their
# answer via a different path (an alt/early completion is not an unmet request).
_MULTI_TURN_TEMPLATE = (
    "Below is the full list of things the user asked for across a conversation, and a "
    "summary of everything the assistant actually did and said. Decide whether any of "
    "the user's asks was left NOT carried out by the end. An ask counts as carried out "
    "if the user got the result they asked for, even if by a different route. Name the "
    "concrete unmet asks; do not suggest how to do them.\n\n"
    "=== what the user asked for (verbatim, in order) ===\n{task}\n\n"
    "=== what the assistant did and said ===\n{output}\n\n"
    "Now judge ONLY the asks above against what the assistant did above: which asks "
    "were never carried out? Output the JSON verdict."
)

# swebench: artifact-correctness.
_SWEBENCH_TEMPLATE = (
    "Below is an issue description and a code change (unified diff) intended to "
    "resolve it. Decide whether the change actually resolves the issue. Name the "
    "concrete ways it fails to resolve the issue, fails a stated requirement, or "
    "introduces a regression; do not propose a corrected patch.\n\n"
    "=== issue (verbatim) ===\n{task}\n\n"
    "=== code change / diff (verbatim) ===\n{output}\n\n"
    "Now judge ONLY the issue above against the diff above: does it resolve the "
    "issue? Output the JSON verdict."
)


def assemble_multi_turn(task: str, output: str) -> str:
    """Sandwich-layout verify prompt for a multi_turn turn (request + response)."""
    return _MULTI_TURN_TEMPLATE.format(task=task.strip(), output=output.strip())


def assemble_swebench(task: str, output: str) -> str:
    """Sandwich-layout verify prompt for a SWE-bench artifact (issue + diff)."""
    return _SWEBENCH_TEMPLATE.format(task=task.strip(), output=output.strip())


_ASSEMBLERS = {"multi_turn": assemble_multi_turn, "swebench": assemble_swebench}


# --- verdict parsing (tolerant; fails CLOSED to gap=False) --------------------

def _iter_balanced(text: str, open_c: str, close_c: str):
    """Yield every balanced top-level <open_c>...<close_c> span (string-aware)."""
    i = 0
    n = len(text)
    while i < n:
        if text[i] == open_c:
            depth = 0
            in_str = False
            esc = False
            for j in range(i, n):
                c = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                else:
                    if c == '"':
                        in_str = True
                    elif c == open_c:
                        depth += 1
                    elif c == close_c:
                        depth -= 1
                        if depth == 0:
                            yield text[i : j + 1]
                            i = j
                            break
        i += 1


def _extract_json(text: str) -> dict[str, Any] | None:
    """The LAST balanced top-level JSON object that parses and carries
    "deficiencies" (the final verdict, after any reasoning drafts); else the last
    parseable object. Reasoning models emit drafts mid-CoT then a final verdict —
    grabbing the FIRST object would catch a draft or an embedded deficiency dict.
    """
    last_obj: dict[str, Any] | None = None
    last_with_key: dict[str, Any] | None = None
    for blob in _iter_balanced(text, "{", "}"):
        try:
            o = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict):
            last_obj = o
            if "deficiencies" in o:
                last_with_key = o
    return last_with_key or last_obj


def _deficiency_list(text: str) -> list[Any] | None:
    """Pull the deficiency list out of a verdict, tolerant of shape.

    Under json_object decoding the model emits either the object
    `{"deficiencies": [...]}` or (occasionally) a bare array `[...]`. Try a whole-text
    parse first (json mode → text IS json), then fall back to the first balanced
    object (for non-json-mode callers). Returns None only if nothing parses.
    """
    stripped = (text or "").strip()
    try:
        whole = json.loads(stripped)
        if isinstance(whole, dict):
            d = whole.get("deficiencies")
            return d if isinstance(d, list) else []
        if isinstance(whole, list):
            return whole
    except json.JSONDecodeError:
        pass
    # Reasoning preamble around the JSON: take the LAST verdict object.
    obj = _extract_json(stripped)
    if obj is not None:
        d = obj.get("deficiencies")
        return d if isinstance(d, list) else []
    # Bare-array fallback (json-mode occasionally emits the list itself).
    arrays = list(_iter_balanced(stripped, "[", "]"))
    for blob in reversed(arrays):
        try:
            arr = json.loads(blob)
            if isinstance(arr, list):
                return arr
        except json.JSONDecodeError:
            continue
    return None


def parse_verdict(text: str) -> Verdict:
    """Parse a verifier's text into a Verdict.

    `gap` is DERIVED: it is True iff there is >=1 substantiated (non-empty `what`)
    functional deficiency. There is no separate model-asserted gap flag to contradict
    the list — the presence of a deficiency IS the gap, which bakes in the
    substantiation guard. Unparseable output fails CLOSED (gap=False) with an error
    set, so a bad verdict never triggers a spurious repair (and the measurement
    script buckets parse errors separately from real abstentions).
    """
    raw_defs = _deficiency_list(text)
    if raw_defs is None:
        return Verdict(gap=False, raw=text or "", error="unparseable_verdict")

    deficiencies: list[Deficiency] = []
    for d in raw_defs:
        if not isinstance(d, dict):
            continue
        what = str(d.get("what", "")).strip()
        if not what:
            continue  # unsubstantiated entry — does not count toward a gap
        spec = str(d.get("specificity", "")).strip().lower()
        if spec not in SPECIFICITY_VALUES:
            spec = "unknown"
        deficiencies.append(Deficiency(
            what=what,
            evidence=str(d.get("evidence", "")).strip(),
            specificity=spec,
        ))

    return Verdict(gap=len(deficiencies) > 0, deficiencies=tuple(deficiencies), raw=text or "")


# --- the verify call ----------------------------------------------------------

def verify(
    backend: Backend,
    *,
    driver: str,
    task: str,
    output: str,
    max_tokens: int = 3000,
    temperature: float = 0.0,
    num_ctx: int | None = None,
) -> Verdict:
    """Run one verify pass. `driver` ∈ {"multi_turn", "swebench"}.

    Pure with respect to the caller's generation transcript — this is a SEPARATE
    chat call; it never mutates the work being judged. Deterministic at temp=0.

    Substrate note (oMLX/MLX champion is a heavy reasoner): json-mode is only weakly
    enforced and the model emits a long CoT preamble before the verdict, so the budget
    must be generous and `parse_verdict` extracts the LAST verdict object after the
    reasoning. `response_format` is kept as a mild nudge toward JSON.
    """
    try:
        assembler = _ASSEMBLERS[driver]
    except KeyError:
        raise ValueError(f"unknown driver {driver!r}; expected one of {sorted(_ASSEMBLERS)}")

    prompt = assembler(task, output)
    try:
        resp = backend.chat(
            messages=[
                {"role": "system", "content": _VERIFIER_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            tools=None,
            max_tokens=max_tokens,
            temperature=temperature,
            num_ctx=num_ctx,
            response_format={"type": "json_object"},
        )
    except Exception as e:  # noqa: BLE001 — surface backend errors as an abstain
        return Verdict(gap=False, error=f"verify_call_failed: {type(e).__name__}: {e}")
    return parse_verdict(resp.text)


# --- shared conversation view (used by live driver + Phase 0/1 scripts) -------

def multi_turn_verify_context(
    transcript: list[dict[str, Any]],
    decoded_turns: list[list[list[str]]],
) -> tuple[str, str]:
    """Build the (task, output) pair for a whole-conversation multi_turn verify.

    Shared by the live driver's verify wiring AND the offline Phase 1 script so the
    "what did the assistant do" view never drifts between analysis and production.
    Robust to message-less reveal turns (no per-turn alignment assumed):
      - `task`   = every user ask, numbered, in order.
      - `output` = the assistant's prose answers + a flat list of the actions it took.
    """
    user_requests = [
        str(m.get("content") or "").strip()
        for m in transcript
        if m.get("role") == "user" and not m.get("_luxe_repair")
        and str(m.get("content") or "").strip()
    ]
    prose = [
        str(m.get("content") or "").strip()
        for m in transcript
        if m.get("role") == "assistant" and str(m.get("content") or "").strip()
    ]
    actions = [c for turn in decoded_turns for step in turn for c in step]

    task = "\n".join(f"{i+1}. {r}" for i, r in enumerate(user_requests)) or "(none)"
    out_parts: list[str] = []
    if prose:
        out_parts.append("Assistant said:\n" + "\n".join(f"- {p}" for p in prose))
    out_parts.append(
        "Actions the assistant performed:\n"
        + ("\n".join(f"- {a}" for a in actions) if actions else "- (none)")
    )
    return task, "\n\n".join(out_parts)


# --- Phase 2 repair: the bounded corrective re-prompt -------------------------

# Generic corrective nudge (NOT a verifier prompt — agents.sdd anti-overfit rule
# governs the verifier surface, not this). It carries NO benchmark semantics: no
# "tool call", no state-checker / turn-path language. It re-states that the request
# is unfinished, names the verifier's own deficiencies (critique, not a solution —
# Phase 2 consumes the verdict by design), and counters the dominant repairable
# give-up mode ("tool-unavailable anchoring") with generic agent guidance.
_REPAIR_NUDGE_BODY = (
    "Your previous response did not fully carry out the request. Re-check the tools "
    "available to you right now and complete what was asked — do not assume a listed "
    "tool is unavailable, and do not stop until the request is done."
)


def repair_nudge(verdict: Verdict) -> str:
    """Build the one corrective re-prompt for a flagged give-up turn (Phase 2).

    Grounds the nudge in the verdict's own deficiencies (the unmet asks the verifier
    cited) so the re-prompt is specific without re-solving the task for the model.
    Caller injects this as a `_luxe_repair`-marked user message — the marker keeps it
    out of any later verify context (`multi_turn_verify_context`).
    """
    asks = "\n".join(f"- {d.what}" for d in verdict.deficiencies if d.what)
    body = _REPAIR_NUDGE_BODY
    if asks:
        body += "\n\nStill not done:\n" + asks
    return body
