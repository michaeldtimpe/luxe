"""gitplan — the apply-ready change-plan artifact (deterministic data shaping).

`gitrefactor` emits a prose structural plan; `gitplan` emits a STRUCTURED plan the
gated executor (`apply.py`) can act on. The champion produces the plan as a fenced
```json block (the GIT_DEEP_REDUCE_HINT precedent); this module parses it leniently,
normalizes it to the `gitplan/v1` schema, renders a human-readable markdown report,
and persists the machine-readable `plan-<head>.json`. No prompt strings live here
(gitkit.sdd Forbids) — only parsing, normalization, ordering, rendering, I/O.

Plan schema (gitplan/v1):
  {schema, head, summary, steps: [
     {id, title, target_files:[...],
      change:{op:extract|move|rename|inline|split|delete, symbols:[...], detail},
      rationale, risk:low|med|high, verify:"<shell cmd or behavior>",
      depends_on:[ids]}]}
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from luxe.gitkit.store import reports_dir

PLAN_SCHEMA = "gitplan/v1"
_VALID_OPS = ("extract", "move", "rename", "inline", "split", "delete")
_VALID_RISK = ("low", "med", "high")
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_plan(text: str) -> dict | None:
    """Lenient extraction of the plan JSON: collect every fenced ```json block plus
    the outer `{...}` span, parse each, return the best dict carrying a `steps` list
    (the one with the most steps). None if nothing parses."""
    if not text:
        return None
    candidates: list[str] = [m.group(1) for m in _JSON_FENCE_RE.finditer(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    best: dict | None = None
    best_score = -1
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get("steps"), list):
            continue
        score = len(obj["steps"])
        if score > best_score:
            best, best_score = obj, score
    return best


def _norm_step(raw: dict, idx: int, default_verify: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title", "")).strip()
    files = [str(f).strip() for f in (raw.get("target_files") or []) if str(f).strip()]
    change = raw.get("change") if isinstance(raw.get("change"), dict) else {}
    detail = str(change.get("detail", "")).strip()
    # Keep a step only if it carries SOME actionable content.
    if not (title or files or detail):
        return None
    op = str(change.get("op", "")).strip().lower()
    op = op if op in _VALID_OPS else "change"
    risk = str(raw.get("risk", "")).strip().lower()
    risk = risk if risk in _VALID_RISK else "med"
    return {
        "id": str(raw.get("id") or f"S{idx + 1}").strip(),
        "title": title or f"Step {idx + 1}",
        "target_files": files,
        "change": {
            "op": op,
            "symbols": [str(s).strip() for s in (change.get("symbols") or [])
                        if str(s).strip()],
            "detail": detail,
        },
        "rationale": str(raw.get("rationale", "")).strip(),
        "risk": risk,
        "verify": str(raw.get("verify", "")).strip() or default_verify,
        "depends_on": [str(d).strip() for d in (raw.get("depends_on") or [])
                       if str(d).strip()],
    }


def normalize_plan(raw: dict | None, *, head: str, summary: str = "",
                   default_verify: str = "") -> dict:
    """Normalize a parsed plan into the gitplan/v1 schema. Fills ids/risk/verify/
    depends_on, drops malformed steps, prunes dangling depends_on, stamps
    schema+head. Never raises — an empty/None input yields a valid empty plan."""
    raw = raw or {}
    steps: list[dict] = []
    for i, s in enumerate(raw.get("steps") or []):
        ns = _norm_step(s, i, default_verify)
        if ns is not None:
            steps.append(ns)
    ids = {s["id"] for s in steps}
    for s in steps:  # drop dependencies on steps that didn't survive
        s["depends_on"] = [d for d in s["depends_on"] if d in ids and d != s["id"]]
    return {
        "schema": PLAN_SCHEMA,
        "head": head or "",
        "summary": str(raw.get("summary") or summary).strip(),
        "steps": steps,
    }


def order_steps(plan: dict) -> list[dict]:
    """Topological order honoring depends_on (stable by id on ties). Raises
    ValueError on a dependency cycle (the executor surfaces it as an abort)."""
    steps = {s["id"]: s for s in plan.get("steps", [])}
    ordered: list[dict] = []
    done: set[str] = set()
    visiting: set[str] = set()

    def visit(sid: str) -> None:
        if sid in done or sid not in steps:
            return
        if sid in visiting:
            raise ValueError(f"dependency cycle at step {sid}")
        visiting.add(sid)
        for dep in steps[sid]["depends_on"]:
            visit(dep)
        visiting.discard(sid)
        done.add(sid)
        ordered.append(steps[sid])

    for sid in sorted(steps):
        visit(sid)
    return ordered


def render_markdown(plan: dict, title: str = "Change plan") -> str:
    """Deterministic human-readable report from the structured plan (mirrors
    deep._render_report — Python never rambles)."""
    steps = plan.get("steps", [])
    summary = plan.get("summary", "") or "no steps"
    out = [f"# {title}", f"**Steps: {len(steps)}** — {summary}", ""]
    for s in steps:
        ch = s["change"]
        out.append(f"## {s['id']}: {s['title']}")
        if s["target_files"]:
            out.append("- **Files:** " + ", ".join(f"`{f}`" for f in s["target_files"]))
        sym = (" — " + ", ".join(f"`{x}`" for x in ch["symbols"])) if ch["symbols"] else ""
        out.append(f"- **Change:** `{ch['op']}`{sym} — {ch['detail']}".rstrip())
        if s["rationale"]:
            out.append(f"- **Rationale:** {s['rationale']}")
        out.append(f"- **Risk:** {s['risk']}")
        if s["verify"]:
            out.append(f"- **Verify:** {s['verify']}")
        if s["depends_on"]:
            out.append("- **Depends on:** " + ", ".join(s["depends_on"]))
        out.append("")
    if not steps:
        out.append("No actionable steps were produced.")
    return "\n".join(out).rstrip() + "\n"


def _plan_path(repo_path: str | Path, head: str) -> Path:
    return reports_dir(repo_path) / f"plan-{head or 'nohead'}.json"


def save_plan_json(repo_path: str | Path, plan: dict) -> Path:
    """Persist the machine-readable plan, keyed by HEAD (latest wins), beside the
    canonical reports under ~/.luxe/reports/<hash>/."""
    p = _plan_path(repo_path, plan.get("head", ""))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan, indent=2))
    return p


def finalize_and_save(repo_path: str | Path, head: str, raw_text: str, *,
                      fallback_steps: list | None = None,
                      extract_fn=None,
                      title: str = "Change plan") -> tuple[str, dict]:
    """Parse the model's plan JSON, normalize it, persist `plan-<head>.json`, and
    return (markdown_report, plan).

    The champion rarely emits clean JSON in an agentic final message, so when the
    raw text has no parseable steps and `extract_fn` is given, run a TRANSCRIPTION
    recovery pass (`extract_fn(raw_text) -> str`) and parse that. `fallback_steps`
    (deep-mode aggregated steps) is the last resort — Python packaging never
    rambles, so a valid plan is always produced."""
    parsed = parse_plan(raw_text)
    if (not parsed or not parsed.get("steps")) and extract_fn is not None:
        try:
            recovered = parse_plan(extract_fn(raw_text) or "")
        except Exception:
            recovered = None
        if recovered and recovered.get("steps"):
            parsed = recovered
    if (not parsed or not parsed.get("steps")) and fallback_steps:
        parsed = {"summary": (parsed or {}).get("summary", ""), "steps": fallback_steps}
    plan = normalize_plan(parsed, head=head)
    save_plan_json(repo_path, plan)
    return render_markdown(plan, title), plan


def latest_plan_for(repo_path: str | Path, head: str) -> dict | None:
    """Return the saved plan for `head` (canonical store), or None."""
    p = _plan_path(repo_path, head)
    if not p.is_file():
        return None
    try:
        plan = json.loads(p.read_text())
    except (ValueError, OSError):
        return None
    return plan if isinstance(plan, dict) and "steps" in plan else None
