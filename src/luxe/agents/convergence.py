"""v1.10 — convergence-score primitive for conditional intervention stacking.

The v1.9 cycle's binary `same_file_read_twice` proxy was empirically too
coarse: full-stack PROTECTED v18 strongs but BROKE some plausibles;
gate-only did the inverse. The Phase D A/B at n=75 showed pure
intervention stacking is non-Pareto — each text-level steer has no
awareness of the others' state or of the model's trajectory shape at
fire time.

v1.10 replaces the binary proxy with a smooth convergence score in
[0.0, 1.0] derived from four signals over the recent tool-call history:

  - repeated_same_path_access  — fraction of reads that revisit a path
  - edit_preview_behavior      — diff/grep/preview observed before write
  - localized_grep_density     — fraction of grep targets in the same
                                 directory as recent reads
  - file_entropy_last_K_events — 1 − normalized Shannon entropy over
                                 the path frequency distribution

Intervention intensity scales with the score:
  score < LOW   — diffuse-recon; suppress commitment intervention
  LOW ≤ s < HIGH — standard soft-anchor / early_bail
  score ≥ HIGH  — tighter commitment phrasing

The function is pure and operates over a list of dicts so it can be
unit-tested in isolation against synthetic trajectories. Loop callers
maintain `tool_history` (last K events) and pass it through.

Tool-history entry shape:
    {
      "step": int,
      "name": str,                   # tool name, lowercased
      "path": str | None,            # path arg if extractable, else None
      "key_hash": str | None,        # _call_key SHA-1 prefix (optional)
    }

Path extraction is permissive — the caller can pass whatever path
proxy is available. For tools that don't have a path (e.g. `bash`),
caller can pass `None`; convergence features treat them as neutral.
"""

from __future__ import annotations

import math
import posixpath
from collections import Counter
from typing import Any, Sequence

_READ_TOOLS = frozenset({"read_file"})
_GREP_TOOLS = frozenset({"grep", "bm25_search"})
_PREVIEW_TOOLS = frozenset({"grep", "bm25_search", "git_diff"})
_WRITE_TOOLS = frozenset({"write_file", "edit_file"})


def _dirname(path: str | None) -> str | None:
    if not path:
        return None
    d = posixpath.dirname(path)
    return d or "."


def _normalized_entropy(paths: Sequence[str | None]) -> float:
    """Shannon entropy of the path distribution, normalized to [0, 1]
    where 0 = all paths identical (max convergence) and 1 = all paths
    unique (max diffuseness). Ignores None entries (non-path tools)."""
    real = [p for p in paths if p]
    if not real:
        return 0.0
    counts = Counter(real)
    n = len(real)
    if n <= 1 or len(counts) == 1:
        return 0.0
    h = 0.0
    for c in counts.values():
        p = c / n
        h -= p * math.log2(p)
    h_max = math.log2(min(n, len(counts)))
    return h / h_max if h_max > 0 else 0.0


def repeated_same_path_access(history: Sequence[dict[str, Any]]) -> float:
    """Fraction of read_file calls that hit a path already seen in the
    recent history. 0.0 = all unique reads; 1.0 = every read was a
    repeat. Strong trajectories empirically show ~3× higher rates than
    empties per the v18 distribution mining."""
    reads = [h for h in history if h.get("name") in _READ_TOOLS and h.get("path")]
    if not reads:
        return 0.0
    unique = len({h["path"] for h in reads})
    return 1.0 - (unique / len(reads))


def edit_preview_behavior(history: Sequence[dict[str, Any]]) -> float:
    """1.0 if any write was immediately preceded by a preview/grep/diff
    in the history window; 0.0 otherwise. Models that "look before they
    leap" are typically committing to a known target — a strong
    convergence signal."""
    for i, h in enumerate(history):
        if h.get("name") not in _WRITE_TOOLS:
            continue
        prev = history[i - 1] if i > 0 else None
        if prev and prev.get("name") in _PREVIEW_TOOLS:
            return 1.0
    return 0.0


def localized_grep_density(history: Sequence[dict[str, Any]]) -> float:
    """Fraction of grep/search targets whose directory matches the
    directory of a recent read_file. High values mean searches are
    localized rather than scattered — a convergence signal. Returns
    0.0 if no greps in history or no reads to compare to."""
    greps = [h for h in history if h.get("name") in _GREP_TOOLS]
    reads = [h for h in history if h.get("name") in _READ_TOOLS]
    if not greps or not reads:
        return 0.0
    read_dirs = {_dirname(h.get("path")) for h in reads}
    read_dirs.discard(None)
    if not read_dirs:
        return 0.0
    localized = 0
    counted = 0
    for g in greps:
        g_dir = _dirname(g.get("path"))
        if g_dir is None:
            continue
        counted += 1
        if g_dir in read_dirs:
            localized += 1
    if counted == 0:
        return 0.0
    return localized / counted


def file_entropy_last_K(history: Sequence[dict[str, Any]],
                        k: int = 10) -> float:
    """Convergence component derived from path entropy. Returns
    `1 − normalized_entropy(last K paths)` so higher values mean more
    convergence (model is touching the same paths repeatedly)."""
    window = list(history)[-k:]
    paths = [h.get("path") for h in window]
    return 1.0 - _normalized_entropy(paths)


# Weights chosen so each signal contributes at most 0.25 to the score;
# convergence is the conjunction of multiple signals (not any one alone).
_DEFAULT_WEIGHTS = {
    "repeated_same_path_access": 0.25,
    "edit_preview_behavior": 0.25,
    "localized_grep_density": 0.25,
    "file_entropy_last_K": 0.25,
}


def compute_convergence_score(
    history: Sequence[dict[str, Any]],
    *,
    weights: dict[str, float] | None = None,
) -> float:
    """Return a smooth convergence score in [0.0, 1.0].

    0.0 = pure diffuse-recon; 1.0 = strongly converged. Linear weighted
    combination of four sub-signals (see module docstring). The
    function is pure — no I/O, no globals, no clock reads — so callers
    can run it on every step without performance concern.

    `history` should contain the agent's tool-call sequence with
    extracted path args. Empty history returns 0.0 (no convergence
    information yet → treat as diffuse / allow standard interventions
    to fire as before).
    """
    if not history:
        return 0.0
    w = weights or _DEFAULT_WEIGHTS
    s = (
        w["repeated_same_path_access"] * repeated_same_path_access(history)
        + w["edit_preview_behavior"] * edit_preview_behavior(history)
        + w["localized_grep_density"] * localized_grep_density(history)
        + w["file_entropy_last_K"] * file_entropy_last_K(history)
    )
    return max(0.0, min(1.0, s))


def extract_path(name: str, args: dict[str, Any]) -> str | None:
    """Permissive path extraction from tool arguments. Returns the
    first available path-like value, or None if the tool's args don't
    contain one. Caller-facing helper so the loop has a single place
    to define "what counts as a path"."""
    if not isinstance(args, dict):
        return None
    for k in ("path", "file_path", "filepath", "filename", "file"):
        v = args.get(k)
        if isinstance(v, str) and v:
            return v
    return None
