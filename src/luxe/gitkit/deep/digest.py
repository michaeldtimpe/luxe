"""gitkit deep mode — Stage 2/3 digest maintenance: parsing a chunk pass's
JSON notes, folding them into the running cross-reference digest, the
deterministic evidence-weighted confidence, merge/compaction under a ceiling,
and the 2-level synthesis reduce."""

from __future__ import annotations

import json
import re

from luxe.agents import prompts
from luxe.context import estimate_tokens
from luxe.gitkit import patterns


# Synthesize-in-two-levels when the aggregate notes exceed this fraction.
_SYNTH_REDUCE_FRAC = 0.80
# Cap on evidence entries kept per merged finding.
_MAX_EVIDENCE_PER_FINDING = 6

_SEVERITY_RANK = patterns.SEVERITY_RANK


def empty_digest() -> dict:
    return {"modules": [], "entities": [], "cross_cutting": [],
            "provisional_findings": [], "markdown_notes": [], "unparsed_chunks": [],
            "steps": []}


# --- chunk-output parsing + digest maintenance ------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_DEEP_KEYS = ("findings", "modules", "entities", "cross_cutting", "steps")


def parse_chunk_notes(text: str) -> dict | None:
    """Parse a chunk pass's JSON output leniently and robustly. The champion
    often wraps the JSON in prose (or emits more than one block), so we collect
    every candidate — all fenced ```json blocks plus the outer `{...}` span — try
    to parse each, and return the best dict (one carrying a recognized deep key,
    preferring the one with the most findings). Returns None if nothing parses."""
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
        if not isinstance(obj, dict):
            continue
        if not any(k in obj for k in _DEEP_KEYS):
            continue
        score = len(obj.get("findings") or []) * 100 + len(obj)
        if score > best_score:
            best, best_score = obj, score
    return best


def _finding_key(f: dict) -> str:
    """Dedup key: prefer root_cause, fall back to title; normalized."""
    base = (f.get("root_cause") or f.get("title") or "").strip().lower()
    return re.sub(r"\s+", " ", base)


def _merge_evidence(a: list, b: list) -> list:
    out: list[str] = []
    for ev in list(a) + list(b):
        ev = str(ev).strip()
        if ev and ev not in out:
            out.append(ev)
        if len(out) >= _MAX_EVIDENCE_PER_FINDING:
            break
    return out


def update_digest(digest: dict, parsed: dict, chunk_index: int) -> dict:
    """Fold one chunk's parsed notes into the running digest (in place)."""
    def _dedupe_extend(key: str, items, name_field: str) -> None:
        existing = {str(e.get(name_field, "")).strip().lower()
                    for e in digest[key] if isinstance(e, dict)}
        for it in items or []:
            if not isinstance(it, dict):
                continue
            nm = str(it.get(name_field, "")).strip().lower()
            if nm and nm not in existing:
                existing.add(nm)
                digest[key].append(it)

    _dedupe_extend("modules", parsed.get("modules"), "name")
    _dedupe_extend("entities", parsed.get("entities"), "name")
    for cc in parsed.get("cross_cutting") or []:
        cc = str(cc).strip()
        if cc and cc not in digest["cross_cutting"]:
            digest["cross_cutting"].append(cc)
    for f in parsed.get("findings") or []:
        if not isinstance(f, dict):
            continue
        rec = dict(f)
        rec["chunk"] = chunk_index
        rec.setdefault("chunks", [chunk_index])   # provenance: contributors
        rec.setdefault("source", "json")          # provenance: parse rung
        rec.setdefault("evidence", [])
        digest["provisional_findings"].append(rec)
    # gitchange: accumulate apply-ready steps (deduped by op + files + title). Empty
    # for gitaudit, so its digest stays byte-identical.
    seen = {(str(s.get("change", {}).get("op", "")),
             tuple(s.get("target_files", []) or []),
             str(s.get("title", "")).strip().lower()) for s in digest["steps"]}
    for s in parsed.get("steps") or []:
        if not isinstance(s, dict):
            continue
        key = (str(s.get("change", {}).get("op", "")),
               tuple(s.get("target_files", []) or []),
               str(s.get("title", "")).strip().lower())
        if key not in seen:
            seen.add(key)
            digest["steps"].append(s)
    return digest


# Provenance rungs, best → worst. Used to keep the BEST source on merge and to
# cap heuristic-salvaged findings at low confidence (provenance-honest).
_SOURCE_RANK = {"json": 3, "md_clean": 2, "md_transcribed": 1, "heuristic": 0}


def _evidence_keys(f: dict) -> set[str]:
    """Normalized `file:line` tokens from a finding's evidence strings
    ("a.py line 12" / "a.py:12" / "a.py 12" → "a.py:12")."""
    keys: set[str] = set()
    for ev in f.get("evidence", []) or []:
        for m in patterns.FILE_LINE_RE.finditer(str(ev)):
            path = m.group("path").lower().removeprefix("./")
            keys.add(f"{path}:{m.group('line')}")
    return keys


def _finding_chunks(f: dict) -> list[int]:
    chunks = list(f.get("chunks") or [])
    if not chunks and "chunk" in f:
        chunks = [f["chunk"]]
    return sorted(set(chunks))


def confidence_of(f: dict) -> tuple[float, str]:
    """Deterministic, EVIDENCE-weighted confidence (never frequency-weighted:
    one hallucinated issue repeated by three chunks must not outscore one real
    issue found once with strong evidence). Weights: +0.5 ≥1 parseable
    file:line; +0.2 ≥2 distinct evidence locations; +0.2 structured/clean
    source (json/md_clean); +0.1 corroborated by ≥2 chunks. heuristic-salvaged
    findings cap at low regardless. Labels: ≥0.7 high, ≥0.4 medium, else low."""
    ev = _evidence_keys(f)
    score = 0.0
    if len(ev) >= 1:
        score += 0.5
    if len(ev) >= 2:
        score += 0.2
    if f.get("source") in ("json", "md_clean"):
        score += 0.2
    if len(_finding_chunks(f)) >= 2:
        score += 0.1
    label = "high" if score >= 0.7 else ("medium" if score >= 0.4 else "low")
    if f.get("source") == "heuristic":
        label = "low"
    return round(score, 2), label


def _sev_rank(f: dict) -> int:
    return _SEVERITY_RANK.get(str(f.get("severity", "")).lower(), 0)


def _merge_into(cur: dict, f: dict) -> None:
    """Merge finding `f` into `cur`: union evidence + chunks, max severity,
    best provenance source."""
    cur["evidence"] = _merge_evidence(cur.get("evidence", []),
                                      f.get("evidence", []))
    cur["chunks"] = sorted(set(_finding_chunks(cur)) | set(_finding_chunks(f)))
    if _SEVERITY_RANK.get(str(f.get("severity", "")).lower(), 0) > \
       _SEVERITY_RANK.get(str(cur.get("severity", "")).lower(), 0):
        cur["severity"] = f.get("severity", cur.get("severity"))
    if _SOURCE_RANK.get(f.get("source", ""), -1) > \
       _SOURCE_RANK.get(cur.get("source", ""), -1):
        cur["source"] = f["source"]


def compact_digest(digest: dict, *, ceiling_tokens: int = 0,
                   log=None) -> dict:
    """Dedupe + merge the digest so it stays under `ceiling_tokens`. Two merge
    passes: (1) same root-cause/title key; (2) EVIDENCE-OVERLAP — findings
    sharing any normalized `file:line` evidence token merge (same bug, different
    words). Merges union evidence + contributing chunks, keep highest severity +
    best provenance source. If still over the ceiling, drops lowest-severity
    findings (logged). Stamps each surviving finding's deterministic
    `confidence`. Returns a new digest dict; never mutates the input."""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for f in digest.get("provisional_findings", []):
        k = _finding_key(f)
        if not k:
            k = f"_anon_{len(order)}"
        if k in merged:
            _merge_into(merged[k], f)
        else:
            merged[k] = dict(f)
            merged[k]["evidence"] = _merge_evidence(f.get("evidence", []), [])
            order.append(k)

    # Pass 2 — evidence-overlap merge (catches same-bug-different-words misses
    # the root-cause key can't).
    by_ev: dict[str, str] = {}          # evidence token -> canonical key
    survivors: list[str] = []
    for k in order:
        f = merged[k]
        hit = next((by_ev[t] for t in _evidence_keys(f) if t in by_ev), None)
        if hit is not None and hit != k:
            _merge_into(merged[hit], f)
            for t in _evidence_keys(merged[hit]):
                by_ev.setdefault(t, hit)
            continue
        survivors.append(k)
        for t in _evidence_keys(f):
            by_ev.setdefault(t, k)

    findings = [merged[k] for k in survivors]
    for f in findings:
        f["confidence_score"], f["confidence"] = confidence_of(f)

    out = {
        "modules": list(digest.get("modules", [])),
        "entities": list(digest.get("entities", [])),
        "cross_cutting": list(digest.get("cross_cutting", [])),
        "provisional_findings": findings,
        "markdown_notes": list(digest.get("markdown_notes", [])),
        "unparsed_chunks": list(digest.get("unparsed_chunks", [])),
        "steps": list(digest.get("steps", [])),
    }

    if ceiling_tokens and estimate_tokens(json.dumps(out)) > ceiling_tokens:
        # Drop lowest-severity findings first until under ceiling — but NEVER
        # one rated above medium: a high/critical finding is worth more than
        # the window it costs, and the synthesis reduce exists for overflow.
        droppable = sorted(
            (f for f in out["provisional_findings"]
             if _sev_rank(f) <= _SEVERITY_RANK["medium"]),
            key=_sev_rank)
        dropped: dict[str, int] = {}
        while droppable and estimate_tokens(json.dumps(out)) > ceiling_tokens:
            victim = droppable.pop(0)
            out["provisional_findings"].remove(victim)
            sev = str(victim.get("severity", "") or "unrated").lower()
            dropped[sev] = dropped.get(sev, 0) + 1
        if log and dropped:
            detail = ", ".join(f"{n} {sev}" for sev, n in dropped.items())
            log(f"digest over budget — dropped {sum(dropped.values())} "
                f"provisional finding(s) ({detail}) to stay in window")
        if log and estimate_tokens(json.dumps(out)) > ceiling_tokens:
            log("digest still over budget after dropping every finding rated "
                "medium or below — kept all high/critical findings")
    return out


def _reduce_findings(digest: dict, *, eff_ctx: int, pass_fn, log=None,
                     role=None) -> dict:
    """2-level reduce: consolidate the aggregate notes — structured
    provisional_findings AND the markdown chunk notes, which are usually the
    bulk of it — in window-sized batches via LLM merge passes at the
    SYNTHESIS window (`role`; it used to run on the chunk role's base window).
    A batch whose pass yields nothing parseable keeps its inputs unchanged,
    so a parse miss never loses findings. Batching sums per-item sizes once
    (it re-measured the whole growing batch per item — quadratic)."""
    findings = list(digest.get("provisional_findings", []))
    md_notes = list(digest.get("markdown_notes", []))
    items: list[tuple[str, dict]] = ([("f", f) for f in findings]
                                     + [("n", n) for n in md_notes])
    batch_budget = int(eff_ctx * _SYNTH_REDUCE_FRAC)
    batches: list[list[tuple[str, dict]]] = []
    cur: list[tuple[str, dict]] = []
    cur_tok = 0
    for it in items:
        cost = estimate_tokens(json.dumps(it[1]))
        if cur and cur_tok + cost > batch_budget:
            batches.append(cur)
            cur, cur_tok = [], 0
        cur.append(it)
        cur_tok += cost
    if cur:
        batches.append(cur)

    survivors: list[dict] = []
    kept_notes: list[dict] = []
    for i, batch in enumerate(batches):
        b_findings = [x for k, x in batch if k == "f"]
        b_notes = [x for k, x in batch if k == "n"]
        if log:
            log(f"reduce batch {i + 1}/{len(batches)} ({len(b_findings)} "
                f"findings, {len(b_notes)} notes)")
        parts = ["<chunk_findings>\nConsolidate these findings:\n"
                 f"{json.dumps({'findings': b_findings}, indent=1)}"]
        if b_notes:
            parts.append("\n\nAdditional per-chunk findings (markdown):\n"
                         + "\n\n".join(
                             f"### chunk {n.get('chunk', 0) + 1} "
                             f"({n.get('label', '')})\n{n.get('md', '')}"
                             for n in b_notes))
        parts.append("\n</chunk_findings>")
        goal = "Consolidate this batch of findings.\n\n" + prompts.GIT_DEEP_REDUCE_HINT
        res = pass_fn(goal, "".join(parts), f"reduce-{i + 1}", role=role)
        parsed = parse_chunk_notes((getattr(res, "final_text", "") or "").strip())
        # an EMPTY findings list from a batch that had inputs is a miss, not
        # "all duplicates": a merge pass cannot legitimately consolidate N
        # real findings into zero, and accepting it dropped the whole batch
        if parsed and isinstance(parsed.get("findings"), list) \
                and parsed["findings"] \
                and not getattr(res, "aborted", False):
            # provenance-honest: a merged finding is only as good as the
            # weakest source that fed its batch
            srcs = [x.get("source", "json") for x in b_findings] + \
                   [x.get("source", "md_clean") for x in b_notes]
            worst = min(srcs, key=lambda s_: _SOURCE_RANK.get(s_, -1)) if srcs else "json"
            for f in parsed["findings"]:
                if isinstance(f, dict):
                    f.setdefault("source", worst)
                    survivors.append(f)
        else:
            survivors.extend(b_findings)     # never lose findings on a parse miss
            kept_notes.extend(b_notes)

    out = dict(digest)
    out["provisional_findings"] = survivors
    out["markdown_notes"] = kept_notes
    return compact_digest(out, ceiling_tokens=0)
